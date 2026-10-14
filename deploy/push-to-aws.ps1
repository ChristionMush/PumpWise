<#
.SYNOPSIS
  Deploy Pumpwise from this PC to the AWS server. Run it in PowerShell from anywhere inside the repo.

.DESCRIPTION
  1. Takes the code that is on GitHub (origin/main by default), not your unsaved local edits.
  2. Refuses if GitHub CI did not pass on that commit (needs the gh CLI; -SkipCiCheck to override).
  3. Packs it, copies it to the server over SSH, and runs deploy/release.sh there, which backs up the
     database, installs, migrates, checks every table exists, restarts and smoke-tests.

  First time, give the server and key; they are remembered in %USERPROFILE%\.pumpwise-deploy.json:
    .\deploy\push-to-aws.ps1 -Server ubuntu@203.0.113.10 -Key C:\Users\you\.ssh\pumpwise.pem
  After that:
    .\deploy\push-to-aws.ps1
  Other uses:
    .\deploy\push-to-aws.ps1 -ListReleases          # what is installed, and the deploy history
    .\deploy\push-to-aws.ps1 -Rollback 2d1a3b7      # go back to an earlier release (code only)
    .\deploy\push-to-aws.ps1 -Ref 2d1a3b7           # deploy a specific commit
    .\deploy\push-to-aws.ps1 -Force                 # deploy even during a shift change
#>
param(
  [string]$Server,
  [string]$Key,
  [string]$Ref = "origin/main",
  [string]$Rollback,
  [switch]$ListReleases,
  [switch]$SkipCiCheck,
  [switch]$Force,
  [string]$RemoteRoot = "/opt/pumpwise"
)
$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host $msg -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

# Server and key: from the command line, else remembered from last time.
$configPath = Join-Path $env:USERPROFILE ".pumpwise-deploy.json"
$config = @{}
if (Test-Path $configPath) {
  $saved = Get-Content $configPath -Raw | ConvertFrom-Json
  $config.Server = $saved.Server; $config.Key = $saved.Key
}
if ($Server) { $config.Server = $Server }
if ($Key) { $config.Key = $Key }
if (-not $config.Server) { Fail "Give the server once, for example: -Server ubuntu@203.0.113.10 -Key C:\path\to\key.pem" }
if ($Server -or $Key) { $config | ConvertTo-Json | Set-Content -Path $configPath -Encoding utf8 }

$sshArgs = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30")
if ($config.Key) {
  if (-not (Test-Path $config.Key)) { Fail "Key file not found: $($config.Key)" }
  $sshArgs += @("-i", $config.Key)
}
foreach ($tool in "git", "ssh", "scp") {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { Fail "$tool is not installed (ssh and scp come with Windows: Settings > System > Optional features > OpenSSH Client)." }
}

function Remote($command) {
  & ssh @sshArgs $config.Server $command
  return $LASTEXITCODE
}

$forceFlag = "0"
if ($Force) { $forceFlag = "1" }

if ($ListReleases) {
  $code = Remote "echo Current: `$(cat $RemoteRoot/releases/CURRENT 2>/dev/null || echo none); echo Installed:; ls -1t $RemoteRoot/releases 2>/dev/null | grep -v -e CURRENT -e history.log; echo History:; tail -n 15 $RemoteRoot/releases/history.log 2>/dev/null"
  exit $code
}

if ($Rollback) {
  if ($Rollback -notmatch '^[0-9a-f]{7,40}$') { Fail "Rollback takes a commit id such as 2d1a3b7 (see -ListReleases)." }
  Step "Rolling back the code to $Rollback on $($config.Server)"
  Write-Host "The database is not rolled back. If a migration must be undone, restore the backup (deploy/restore.sh)." -ForegroundColor Yellow
  $code = Remote "test -d $RemoteRoot/releases/$Rollback || { echo 'No release $Rollback on the server. Use -ListReleases.'; exit 1; }; FORCE=$forceFlag bash $RemoteRoot/releases/$Rollback/deploy/release.sh $Rollback"
  exit $code
}

$repo = Split-Path $PSScriptRoot -Parent
Push-Location $repo
try {
  Step "1/4 Fetch the latest code from GitHub"
  git fetch --quiet origin
  if ($LASTEXITCODE -ne 0) { Fail "git fetch failed. Check your internet connection and GitHub access." }
  $full = (git rev-parse --verify "$Ref^{commit}").Trim()
  if ($LASTEXITCODE -ne 0 -or -not $full) { Fail "Unknown commit or branch: $Ref" }
  $sha = $full.Substring(0, 7)
  $subject = (git log -1 --format=%s $full).Trim()
  Write-Host "Deploying $sha  $subject"
  $ahead = (git rev-list --count "origin/main..HEAD").Trim()
  if ($Ref -eq "origin/main" -and $ahead -ne "0") {
    Write-Host "Note: you have $ahead local commit(s) not on GitHub. They are NOT deployed. Run 'git push' first if you want them." -ForegroundColor Yellow
  }

  Step "2/4 Check GitHub CI passed on $sha"
  if ($SkipCiCheck) {
    Write-Host "Skipped (-SkipCiCheck)." -ForegroundColor Yellow
  } elseif (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Fail "The gh CLI is not installed, so CI cannot be checked. Install it (winget install GitHub.cli) or use -SkipCiCheck."
  } else {
    $runs = gh run list --commit $full --workflow CI --json status,conclusion --limit 1 | ConvertFrom-Json
    if (-not $runs) { Fail "No CI run found for $sha. Push it to GitHub and wait for CI, or use -SkipCiCheck." }
    if ($runs[0].status -ne "completed") { Fail "CI is still running on $sha. Wait for it (gh run watch), then deploy." }
    if ($runs[0].conclusion -ne "success") { Fail "CI did not pass on $sha ($($runs[0].conclusion)). Fix it before deploying." }
    Write-Host "CI passed."
  }

  Step "3/4 Pack and copy to $($config.Server)"
  $tarball = Join-Path $env:TEMP "pumpwise-$sha.tar.gz"
  # core.autocrlf=false: ship the files exactly as stored in Git, so shell scripts keep Linux line endings.
  git -c core.autocrlf=false archive --format=tar.gz -o $tarball $full
  if ($LASTEXITCODE -ne 0) { Fail "git archive failed." }
  & scp @sshArgs $tarball "$($config.Server):/tmp/pumpwise-$sha.tar.gz"
  if ($LASTEXITCODE -ne 0) { Fail "Copy to the server failed. Check -Server, -Key, and that SSH (port 22) is open to your IP in the security group." }
  Remove-Item $tarball -ErrorAction SilentlyContinue

  Step "4/4 Install on the server (backup, build, migrate, restart, smoke test)"
  $code = Remote "set -e; R=$RemoteRoot/releases/$sha; mkdir -p `$R; tar -xzf /tmp/pumpwise-$sha.tar.gz -C `$R; rm -f /tmp/pumpwise-$sha.tar.gz; FORCE=$forceFlag bash `$R/deploy/release.sh $sha"
  if ($code -ne 0) { Fail "Deploy of $sha did not finish cleanly (see above)." }
  Write-Host "`nDone: $sha is live." -ForegroundColor Green
} finally {
  Pop-Location
}
