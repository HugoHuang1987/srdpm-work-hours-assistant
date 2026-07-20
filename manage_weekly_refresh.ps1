[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("Install", "Uninstall", "Status")]
    [string]$Action = "Status"
)

# 管理“每周一 10:00 只读同步 Wiki、刷新近两月、不审批”的当前用户计划任务。
# 安装本身只写入 Task Scheduler 配置，绝不立即联网或触发刷新。
$ErrorActionPreference = "Stop"
$taskName = "SRDPM-WeeklyDashboardRefresh"
$taskPath = [string][char]92
$projectDir = (Resolve-Path -LiteralPath $PSScriptRoot -ErrorAction Stop).Path
$refreshScript = (Resolve-Path -LiteralPath (Join-Path $projectDir "refresh_dashboard.py") -ErrorAction Stop).Path
$runAsUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function Quote-WindowsArgument {
    param([Parameter(Mandatory = $true)][string]$Value)

    $doubleQuote = [string][char]34
    if ($Value.Contains($doubleQuote)) {
        throw "Windows 启动参数不能包含双引号。"
    }
    return $doubleQuote + $Value + $doubleQuote
}

function Get-PythonExecutable {
    $candidates = @()
    $pythonExe = Get-Command -Name 'python.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    $python = Get-Command -Name 'python' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $pythonExe -and -not [string]::IsNullOrWhiteSpace($pythonExe.Source)) {
        $candidates += $pythonExe
    }
    if ($null -ne $python -and -not [string]::IsNullOrWhiteSpace($python.Source)) {
        $candidates += $python
    }

    foreach ($candidate in $candidates) {
        try {
            # Ask the launcher for its real interpreter path.  This avoids storing
            # a WindowsApps alias in the task action when a concrete Python exists.
            $reported = @(& $candidate.Source -c 'import sys; print(sys.executable)' 2>$null) |
                Select-Object -Last 1
            if ([string]::IsNullOrWhiteSpace([string]$reported)) {
                continue
            }
            $resolved = (Resolve-Path -LiteralPath ([string]$reported) -ErrorAction Stop).Path
            if (Test-Path -LiteralPath $resolved -PathType Leaf) {
                return $resolved
            }
        }
        catch {
            # Try the next configured launcher; no task is changed on probe failure.
        }
    }
    throw "未找到可运行的 Python 解释器；未安装或修改计划任务。"
}

function Get-ExistingTask {
    try {
        return Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function New-WeeklyRefreshDefinition {
    $pythonExecutable = Get-PythonExecutable
    $action = New-ScheduledTaskAction `
        -Execute $pythonExecutable `
        -Argument (Quote-WindowsArgument $refreshScript) `
        -WorkingDirectory $projectDir
    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -DaysOfWeek Monday `
        -At ([DateTime]::Today.AddHours(10))
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 90)
    # ScheduledTasks 的 RunLevelEnum 用 Limited 表示最低权限。
    $principal = New-ScheduledTaskPrincipal `
        -UserId $runAsUser `
        -LogonType Interactive `
        -RunLevel Limited
    return [PSCustomObject]@{
        Action    = $action
        Trigger   = $trigger
        Settings  = $settings
        Principal = $principal
    }
}

function Show-WeeklyRefreshStatus {
    $task = Get-ExistingTask
    if ($null -eq $task) {
        [PSCustomObject]@{
            Installed          = $false
            TaskName           = $taskName
            Schedule           = "Monday 10:00"
            ApprovalOperations = "none"
        }
        return
    }

    $info = Get-ScheduledTaskInfo -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    $taskAction = @($task.Actions) | Select-Object -First 1
    $taskTrigger = @($task.Triggers) | Select-Object -First 1
    [PSCustomObject]@{
        Installed          = $true
        TaskName           = $taskName
        State              = [string]$task.State
        RunAs              = [string]$task.Principal.UserId
        LogonType          = [string]$task.Principal.LogonType
        RunLevel           = [string]$task.Principal.RunLevel
        Schedule           = "Monday 10:00"
        TriggerStart       = [string]$taskTrigger.StartBoundary
        StartWhenAvailable = [bool]$task.Settings.StartWhenAvailable
        # Task Scheduler stores these two switches as inverse properties.
        AllowStartIfOnBatteries = -not [bool]$task.Settings.DisallowStartIfOnBatteries
        DontStopIfGoingOnBatteries = -not [bool]$task.Settings.StopIfGoingOnBatteries
        MultipleInstances  = [string]$task.Settings.MultipleInstances
        Executable         = [string]$taskAction.Execute
        Arguments          = [string]$taskAction.Arguments
        WorkingDirectory   = [string]$taskAction.WorkingDirectory
        NextRunTime        = if ($null -ne $info) { $info.NextRunTime } else { $null }
        LastRunTime        = if ($null -ne $info) { $info.LastRunTime } else { $null }
        LastTaskResult     = if ($null -ne $info) { $info.LastTaskResult } else { $null }
        ApprovalOperations = "none"
    }
}

switch ($Action) {
    "Install" {
        $definition = New-WeeklyRefreshDefinition
        Register-ScheduledTask `
            -TaskName $taskName `
            -TaskPath $taskPath `
            -Action $definition.Action `
            -Trigger $definition.Trigger `
            -Settings $definition.Settings `
            -Principal $definition.Principal `
            -Description "每周一 10:00 只读同步 Wiki 允许机芯，拉取当前月及前一月 SRDPM 数据并刷新本地工时看板；不执行审批。" `
            -Force | Out-Null
        Write-Output "已安装当前用户周一 10:00 刷新任务；安装未触发网络访问。"
    }
    "Uninstall" {
        if ($null -ne (Get-ExistingTask)) {
            Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
        }
        Write-Output "已移除周一刷新任务。"
    }
    "Status" {
        Show-WeeklyRefreshStatus
    }
}
