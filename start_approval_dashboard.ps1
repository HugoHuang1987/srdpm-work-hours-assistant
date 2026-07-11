$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$passwordWasProvided = -not [string]::IsNullOrEmpty($env:SRDPM_PASSWORD)

try {
    if ([string]::IsNullOrWhiteSpace($env:SRDPM_USERNAME)) {
        $env:SRDPM_USERNAME = Read-Host "请输入 SRDPM 用户名"
    }
    if ([string]::IsNullOrWhiteSpace($env:SRDPM_USERNAME)) {
        throw "SRDPM 用户名不能为空。"
    }

    if (-not $passwordWasProvided) {
        $securePassword = Read-Host "请输入 SRDPM 密码（仅本次运行保存在进程内存）" -AsSecureString
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        try {
            $env:SRDPM_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        }
        finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
    }
    if ([string]::IsNullOrEmpty($env:SRDPM_PASSWORD)) {
        throw "SRDPM 密码不能为空。"
    }

    $python = Get-Command python -ErrorAction Stop
    Push-Location $projectDir
    try {
        & $python.Source ".\local_approval_server.py" --open
        if ($LASTEXITCODE -ne 0) {
            throw "本机审批服务启动失败（退出码 $LASTEXITCODE）。"
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    Write-Host ""
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host "请确认已安装 requirements.txt 中的依赖。" -ForegroundColor Yellow
    Read-Host "按 Enter 退出"
    exit 1
}
finally {
    if (-not $passwordWasProvided) {
        Remove-Item Env:SRDPM_PASSWORD -ErrorAction SilentlyContinue
    }
}
