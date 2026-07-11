[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("Install", "Uninstall", "Start", "Stop", "Status")]
    [string]$Action = "Status",
    [switch]$ConfirmStop
)

$ErrorActionPreference = "Stop"
$runKeyPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$runValueName = "SRDPMApprovalService"
$servicePort = 8765
$serviceOrigin = "http://127.0.0.1:$servicePort"
$configPattern = '<script id="srdpm-local-service-config" type="application/json">(?<json>.*?)</script>'
$projectDir = (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
$serverPath = (Resolve-Path -LiteralPath (Join-Path $projectDir "local_approval_server.py") -ErrorAction Stop).Path
$backgroundLauncher = (Resolve-Path -LiteralPath (Join-Path $projectDir "start_approval_service_background.ps1") -ErrorAction Stop).Path
$powershellExe = Join-Path $PSHOME "powershell.exe"

if (-not (Test-Path -LiteralPath $powershellExe -PathType Leaf)) {
    throw "未找到 Windows PowerShell：$powershellExe"
}

function Quote-WindowsArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Contains('"')) {
        throw "Windows 启动参数不能包含双引号。"
    }
    return '"' + $Value + '"'
}

function Get-ExpectedRunCommand {
    return (
        (Quote-WindowsArgument $powershellExe) +
        " -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " +
        (Quote-WindowsArgument $backgroundLauncher)
    )
}

function Get-InstalledRunCommand {
    try {
        return (Get-ItemPropertyValue -LiteralPath $runKeyPath -Name $runValueName -ErrorAction Stop)
    }
    catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    }
    catch [System.Management.Automation.PSArgumentException] {
        return $null
    }
}

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

function Get-ApprovalServiceProcesses {
    $escapedServerPath = [Regex]::Escape($serverPath)
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -match '^pythonw?(?:\d+(?:\.\d+)*)?\.exe$' -and
            -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
            $_.CommandLine -match $escapedServerPath
        }
    )
}

function Get-ApprovalWatchdogProcesses {
    $escapedLauncherPath = [Regex]::Escape($backgroundLauncher)
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -match '^(?:powershell|pwsh)\.exe$' -and
            -not [string]::IsNullOrWhiteSpace($_.CommandLine) -and
            $_.CommandLine -match $escapedLauncherPath
        }
    )
}

function Start-ApprovalWatchdog {
    $quotedLauncherPath = Quote-WindowsArgument $backgroundLauncher
    return Start-Process `
        -FilePath $powershellExe `
        -ArgumentList @(
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-File", $quotedLauncherPath
        ) `
        -WorkingDirectory $projectDir `
        -WindowStyle Hidden `
        -PassThru
}

function Show-ApprovalServiceStatus {
    $expectedCommand = Get-ExpectedRunCommand
    $installedCommand = Get-InstalledRunCommand
    $serviceProcesses = @(Get-ApprovalServiceProcesses)
    $watchdogProcesses = @(Get-ApprovalWatchdogProcesses)

    [PSCustomObject]@{
        AutoStartInstalled = ($null -ne $installedCommand)
        AutoStartCurrent   = ($installedCommand -ceq $expectedCommand)
        ServiceResponding  = (Test-ExpectedApprovalService)
        ExpectedBuildId    = (Get-ExpectedServiceBuildId)
        WatchdogProcessIds = (($watchdogProcesses | ForEach-Object { $_.ProcessId }) -join ",")
        ServiceProcessIds  = (($serviceProcesses | ForEach-Object { $_.ProcessId }) -join ",")
        Origin             = $serviceOrigin
        RunEntry           = $runValueName
    }
}

switch ($Action) {
    "Install" {
        if (-not (Test-Path -LiteralPath $runKeyPath)) {
            New-Item -Path $runKeyPath -Force | Out-Null
        }
        $expectedCommand = Get-ExpectedRunCommand
        $installedCommand = Get-InstalledRunCommand
        if ($installedCommand -cne $expectedCommand) {
            New-ItemProperty `
                -LiteralPath $runKeyPath `
                -Name $runValueName `
                -PropertyType String `
                -Value $expectedCommand `
                -Force | Out-Null
        }
        Write-Output "已安装当前用户登录自启动项（无需管理员权限）。"
    }
    "Uninstall" {
        if ($null -ne (Get-InstalledRunCommand)) {
            Remove-ItemProperty -LiteralPath $runKeyPath -Name $runValueName -ErrorAction Stop
        }
        Write-Output "已移除当前用户登录自启动项；正在运行的服务不受影响。"
    }
    "Start" {
        $watchdogs = @(Get-ApprovalWatchdogProcesses)
        $startedWatchdog = $null
        if ($watchdogs.Count -eq 0) {
            $startedWatchdog = Start-ApprovalWatchdog
        }

        $serviceReady = $false
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            if (Test-ExpectedApprovalService) {
                $serviceReady = $true
                break
            }
            if ($null -ne $startedWatchdog) {
                $startedWatchdog.Refresh()
                if ($startedWatchdog.HasExited) {
                    throw "SRDPM 审批服务 watchdog 启动后立即退出（退出码 $($startedWatchdog.ExitCode)）。"
                }
            }
            Start-Sleep -Milliseconds 250
        }
        if (-not $serviceReady) {
            if (Test-LocalPortInUse) {
                throw "watchdog 已运行，但端口 $servicePort 被旧版本服务或其他程序占用；为防止并行审批，未启动当前服务。"
            }
            throw "watchdog 已运行，但当前版本 SRDPM 审批服务在 10 秒内未就绪。"
        }
        Write-Output "SRDPM 审批 watchdog 已在后台运行，当前服务可用：$serviceOrigin"
    }
    "Stop" {
        if (-not $ConfirmStop) {
            throw "停止后台服务可能中断正在进行的不可撤回审批。请先确认页面没有运行中的审批任务，再显式添加 -ConfirmStop。"
        }

        # 必须先停常驻 watchdog，防止它在服务停止后立即重新拉起。
        $watchdogs = @(Get-ApprovalWatchdogProcesses)
        foreach ($watchdog in $watchdogs) {
            Stop-Process -Id $watchdog.ProcessId -Force -ErrorAction Stop
        }

        # 只停止命令行包含本项目绝对 server 路径的 Python 进程。
        $serviceProcesses = @(Get-ApprovalServiceProcesses)
        foreach ($serviceProcess in $serviceProcesses) {
            Stop-Process -Id $serviceProcess.ProcessId -Force -ErrorAction Stop
        }

        if ($watchdogs.Count -eq 0 -and $serviceProcesses.Count -eq 0) {
            Write-Output "未发现本项目的 watchdog 或 SRDPM 审批服务进程。"
        }
        else {
            $watchdogIds = (($watchdogs | ForEach-Object { $_.ProcessId }) -join ',')
            $serviceIds = (($serviceProcesses | ForEach-Object { $_.ProcessId }) -join ',')
            Write-Output "已停止 SRDPM 审批后台进程（watchdog=$watchdogIds；service=$serviceIds）。"
        }
    }
    "Status" {
        Show-ApprovalServiceStatus
    }
}
