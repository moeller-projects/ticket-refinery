# run.ps1 — thin launcher for ticket-refinery.
# Ponytail: this script has no logic of its own. Build/run/exit-code propagation only.
[CmdletBinding()]
param(
    [switch]$UseRemoteImage,
    [switch]$Build
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path '.env')) {
    Write-Error "Missing .env. Copy .env.example to .env and fill in values."
    exit 2
}

$engine = $env:CONTAINER_ENGINE
if (-not $engine) {
    if (Get-Command docker -ErrorAction SilentlyContinue) { $engine = 'docker' }
    elseif (Get-Command podman -ErrorAction SilentlyContinue) { $engine = 'podman' }
    else {
        Write-Error "No container engine on PATH. Set CONTAINER_ENGINE or install docker/podman."
        exit 2
    }
}

$localTag  = 'ticket-refinery:latest'
$remoteTag = 'ghcr.io/moeller-projects/ticket-refinery:latest'

if ($UseRemoteImage) {
    & $engine pull $remoteTag
    $image = $remoteTag
} else {
    $existing = & $engine images -q $localTag 2>$null
    if ($Build -or -not $existing) {
        & $engine build -t $localTag .
    }
    $image = $localTag
}

# Mount Pi's auth.json if present on the host, so subscription OAuth tokens
# (ChatGPT Plus/Pro Codex, Claude Pro/Max, GitHub Copilot, …) are visible
# inside the container. Pi reads /root/.pi/agent/auth.json because the image
# runs as root. Read-write: subscription OAuth auto-refresh writes back here,
# and `:ro` would break token renewal on long runs.
$runArgs = @('run', '--rm')
if ($engine -eq 'podman') {
    # ponytail: podman rootless (slirp4netns) often fails DNS resolution inside
    # the container — force public DNS so dev.azure.com / github.com resolve.
    $runArgs += @('--network', 'bridge', '--dns', '8.8.8.8', '--dns', '1.1.1.1')
}
$runArgs += @(
    '--env-file', '.env',
    '--volume', "${PWD}/src/repos.jsonc:/app/src/repos.jsonc:ro"
)
$authJsonPath = Join-Path $HOME '.pi/agent/auth.json'
if (Test-Path $authJsonPath) {
    Write-Host "Mounting Pi auth.json: $authJsonPath"
    $runArgs += @('--volume', "${authJsonPath}:/root/.pi/agent/auth.json")
}
$runArgs += @($image)

& $engine @runArgs
exit $LASTEXITCODE