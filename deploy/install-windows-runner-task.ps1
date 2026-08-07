[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = 'AutoCodeLocalWebRunner'
)

$ErrorActionPreference = 'Stop'

$resolvedRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = Join-Path $resolvedRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Runner Python executable does not exist: $python"
}

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$actionParams = @{
    Execute          = $python
    Argument         = '-m autocode.web.runner'
    WorkingDirectory = $resolvedRoot
}
$action = New-ScheduledTaskAction @actionParams
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$recoveryTriggerParams = @{
    Once               = $true
    At                 = (Get-Date).AddMinutes(1)
    RepetitionInterval = (New-TimeSpan -Minutes 1)
    RepetitionDuration = (New-TimeSpan -Days 3650)
}
$recoveryTrigger = New-ScheduledTaskTrigger @recoveryTriggerParams
$settingsParams = @{
    AllowStartIfOnBatteries     = $true
    DontStopIfGoingOnBatteries  = $true
    StartWhenAvailable          = $true
    ExecutionTimeLimit          = [TimeSpan]::Zero
    MultipleInstances           = 'IgnoreNew'
    RestartCount                = 999
    RestartInterval             = (New-TimeSpan -Minutes 1)
}
$settings = New-ScheduledTaskSettingsSet @settingsParams
$principalParams = @{
    UserId   = $userId
    LogonType = 'Interactive'
    RunLevel = 'Limited'
}
$principal = New-ScheduledTaskPrincipal @principalParams

$taskParams = @{
    Action      = $action
    Trigger     = @($logonTrigger, $recoveryTrigger)
    Settings    = $settings
    Principal   = $principal
    Description = 'Keeps the AutoCode local Web Runner connected to its Relay.'
}
$task = New-ScheduledTask @taskParams

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
