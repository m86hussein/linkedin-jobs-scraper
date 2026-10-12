# One-time setup of the LinkedIn scraper agent on a new Windows machine.
# Run from the repo root:  powershell -ExecutionPolicy Bypass -File agent\setup-agent.ps1 [-InstallTask]
param([switch]$InstallTask)
$ErrorActionPreference = 'Stop'
$agent = $PSScriptRoot
$key = Join-Path $HOME '.ssh\jobsagent_ed25519'

foreach ($cmd in 'uv', 'ssh', 'ssh-keygen') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd not found. Install uv: https://docs.astral.sh/uv/  (ssh ships with Windows 10/11)" }
}

# 1. SSH key for the restricted tunnel user. Empty passphrase so the agent can start unattended.
if (-not (Test-Path $key)) {
    New-Item -ItemType Directory -Force (Split-Path $key) | Out-Null
    # Raw command line: PowerShell 5.1 drops '' args and 7.x passes '""' literally, so -N can't be passed portably.
    $p = Start-Process ssh-keygen -ArgumentList "-q -t ed25519 -N `"`" -C jobsagent@$env:COMPUTERNAME -f `"$key`"" -Wait -NoNewWindow -PassThru
    if ($p.ExitCode) { throw 'ssh-keygen failed' }
}
$p = Start-Process ssh-keygen -ArgumentList "-y -P `"`" -f `"$key`"" -Wait -NoNewWindow -PassThru -RedirectStandardOutput "$env:TEMP\jobsagent.pub.check"
if ($p.ExitCode) { throw "$key has a passphrase; delete it and re-run" }

# 2. Database password, kept in a file only this Windows user can read.
$pwFile = Join-Path $agent '.db_password'
if (-not (Test-Path $pwFile)) {
    $sec = Read-Host 'Paste the linkedin_scraper DB password' -AsSecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
    [IO.File]::WriteAllText($pwFile, $plain.Trim())
    & icacls $pwFile /inheritance:r /grant:r "${env:USERNAME}:(R,W)" | Out-Null
}

# 3. Per-machine config (not committed).
[ordered]@{
    agent_name = $env:COMPUTERNAME
    ssh        = @{ host = 'jobsagent@your-server.example.com'; key = $key }
    db         = @{ password_file = $pwFile }
} | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $agent 'config.local.json')

# 4. Optional: start the agent automatically when this user logs on.
if ($InstallTask) {
    $action = New-ScheduledTaskAction -Execute (Get-Command uv).Source `
        -Argument 'run --with-requirements requirements.txt python agent.py' -WorkingDirectory $agent
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName 'LinkedInJobsAgent' -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Host 'Scheduled task "LinkedInJobsAgent" installed (starts at logon).'
}

$ip = (Invoke-RestMethod https://checkip.amazonaws.com).Trim()
Write-Host "`nSend these two lines to the admin to enable this machine:" -ForegroundColor Yellow
Write-Host "  Public IP : $ip"
Write-Host "  Public key: $((Get-Content "$key.pub" -Raw).Trim())"
Write-Host "`nThen sign in to LinkedIn once on this machine:" -ForegroundColor Yellow
Write-Host '  uvx mcp-server-linkedin==4.26.0 --login --login-timeout 0'
Write-Host "`nAnd start the agent (from $agent):" -ForegroundColor Yellow
Write-Host '  uv run --with-requirements requirements.txt python agent.py'
