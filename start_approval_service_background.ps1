[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$servicePort = 8765
$serviceOrigin = "http://127.0.0.1:$servicePort"
$configPattern = '<script id="srdpm-local-service-config" type="application/json">(?<json>.*?)</script>'
$projectDir = (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
$serverPath = (Resolve-Path -LiteralPath (Join-Path $projectDir "local_approval_server.py") -ErrorAction Stop).Path
$watchdogMutex = $null
$ownsWatchdogMutex = $false

function Get-ExpectedServiceBuildId {
    $hash = (Get-FileHash -LiteralPath $serverPath -Algorithm SHA256 -ErrorAction Stop).Hash
    if ($hash -notmatch '^[0-9A-Fa-f]{64}$') {
        throw "无法计算 local_approval_server.py 的有效 SHA256。"
    }
    return $hash.Substring(0, 16).ToLowerInvariant()
}

function Test-ExpectedApprovalService {
    try {
        $response = Invoke-WebRequest `
            -Uri ($serviceOrigin + "/") `
            -UseBasicParsing `
            -TimeoutSec 2 `
            -ErrorAction Stop
        if ($response.StatusCode -ne 200) {
            return $false
        }

        $match = [Regex]::Match(
            $response.Content,
            $configPattern,
            [Text.RegularExpressions.RegexOptions]::Singleline -bor
                [Text.RegularExpressions.RegexOptions]::CultureInvariant
        )
        if (-not $match.Success) {
            return $false
        }
        $config = $match.Groups["json"].Value | ConvertFrom-Json -ErrorAction Stop
        $actualBuildId = [string]$config.service_build_id
        $expectedBuildId = Get-ExpectedServiceBuildId
        return (
            $actualBuildId -cmatch '^[0-9a-f]{16}$' -and
            $actualBuildId -ceq $expectedBuildId
        )
    }
    catch {
        return $false
    }
}

function Test-LocalPortInUse {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $asyncResult = $client.BeginConnect("127.0.0.1", $servicePort, $null, $null)
        if (-not $asyncResult.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-PythonWindowlessExecutable {
    $pythonw = Get-Command "pythonw.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pythonw -and -not [string]::IsNullOrWhiteSpace($pythonw.Source)) {
        return $pythonw.Source
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $python -or [string]::IsNullOrWhiteSpace($python.Source)) {
        throw "未找到 pythonw.exe 或 python.exe。请先安装 Python 并加入 PATH。"
    }

    $siblingPythonw = Join-Path (Split-Path -Parent $python.Source) "pythonw.exe"
    if (Test-Path -LiteralPath $siblingPythonw -PathType Leaf) {
        return $siblingPythonw
    }
    return $python.Source
}

function Start-CurrentApprovalService {
    $pythonExecutable = Get-PythonWindowlessExecutable
    # ArgumentList 会合并成 Windows 命令行；保留绝对脚本路径两侧引号以支持中文和空格。
    $quotedServerPath = '"' + $serverPath + '"'
    return Start-Process `
        -FilePath $pythonExecutable `
        -ArgumentList @($quotedServerPath, "--quiet") `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -PassThru
}

try {
    # 此互斥锁由常驻 watchdog 持有；多次登录触发或人工 Start 只会保留一个 watchdog。
    $watchdogMutex = New-Object System.Threading.Mutex($false, "Local\SRDPMApprovalServiceWatchdog")
    try {
        $ownsWatchdogMutex = $watchdogMutex.WaitOne(0, $false)
    }
    catch [System.Threading.AbandonedMutexException] {
        $ownsWatchdogMutex = $true
    }
    if (-not $ownsWatchdogMutex) {
        exit 0
    }

    while ($true) {
        try {
            if (Test-ExpectedApprovalService) {
                Start-Sleep -Seconds 5
                continue
            }

            # 端口仍开放但 build id 不匹配时，可能是旧服务或其他程序。绝不并行启动。
            if (Test-LocalPortInUse) {
                Start-Sleep -Seconds 5
                continue
            }

            $serverProcess = Start-CurrentApprovalService
            for ($attempt = 0; $attempt -lt 40; $attempt++) {
                Start-Sleep -Milliseconds 200
                if (Test-ExpectedApprovalService) {
                    break
                }
                $serverProcess.Refresh()
                if ($serverProcess.HasExited) {
                    break
                }
            }
        }
        catch {
            # 后台 watchdog 不弹窗；保留进程并在下一轮重试可恢复的启动故障。
        }
        Start-Sleep -Seconds 5
    }
}
finally {
    if ($ownsWatchdogMutex -and $null -ne $watchdogMutex) {
        try {
            $watchdogMutex.ReleaseMutex()
        }
        catch {
            # 释放失败不应覆盖原始错误。
        }
    }
    if ($null -ne $watchdogMutex) {
        $watchdogMutex.Dispose()
    }
}
