# check.ps1 — preflight diagnostic for ticket-refinery.
# Catches the failure modes we've actually hit: missing .env, inline # comment
# pollution, missing image, no container engine, no Pi auth.json, bad PAT scope.
[CmdletBinding()]
param(
    [switch]$SkipNetwork
)

$failed = $false

function Test-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    $mark = if ($Ok) { '[ OK ]' } else { '[FAIL]'; $script:failed = $true }
    Write-Host ("{0,-7} {1,-44} {2}" -f $mark, $Name, $Detail)
}

# --- .env ---
$envPath = '.env'
if (-not (Test-Path $envPath)) {
    Test-Check '.env present' $false 'run: cp .env.example .env'
    exit 1
}
Test-Check '.env present' $true

# Parse .env. No inline # comment stripping here — that's what we want to flag.
$env = @{}
Get-Content $envPath | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*?)\s*=\s*(.*)$') {
        $env[$matches[1].Trim()] = $matches[2].Trim()
    }
}

# --- required vars ---
$required = @(
    'ADO_ORG','ADO_PROJECT','ADO_PAT',
    'TAG_TRIGGER','TAG_DONE','TAG_BLOCKED',
    'MARKER_FIELD','CLONE_DEPTH','PI_MODEL','PI_PERMISSIONS_FILE'
)
foreach ($k in $required) {
    $v = $env[$k]
    $ok = -not [string]::IsNullOrEmpty($v)
    Test-Check "env: $k" $ok ($(if ($ok) { "$($v.Length) chars" } else { 'MISSING' }))
}

# --- inline # comment pollution (the bug from this session) ---
foreach ($k in @('ADO_PAT','ADO_ORG','ADO_PROJECT','PI_MODEL','MARKER_FIELD')) {
    $v = $env[$k]
    if ($v -and $v -match '\s#') {
        Test-Check "env: $k clean" $false 'trailing # comment — strip it from .env'
    } else {
        Test-Check "env: $k clean" $true
    }
}

# --- repos.jsonc ---
$reposPath = 'src/repos.jsonc'
Test-Check 'repos.jsonc present' (Test-Path $reposPath)

# --- container engine ---
$engine = $env:CONTAINER_ENGINE
if (-not $engine) {
    if (Get-Command docker -ErrorAction SilentlyContinue) { $engine = 'docker' }
    elseif (Get-Command podman -ErrorAction SilentlyContinue) { $engine = 'podman' }
}
if ($engine) {
    Test-Check 'container engine' $true $engine
    $img = & $engine images -q 'ticket-refinery:latest' 2>$null
    Test-Check 'image: ticket-refinery:latest' (-not [string]::IsNullOrEmpty($img))
} else {
    Test-Check 'container engine' $false 'install docker/podman or set CONTAINER_ENGINE'
}

# --- Pi auth.json (for subscription OAuth) ---
$authPath = Join-Path $HOME '.pi/agent/auth.json'
Test-Check 'Pi auth.json' (Test-Path $authPath) $authPath

# --- ADO connectivity ---
if (-not $SkipNetwork -and $env['ADO_PAT'] -and $env['ADO_ORG'] -and $env['ADO_PROJECT']) {
    Write-Host "`n--- ADO connectivity ---"
    $cred = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes(":$($env['ADO_PAT'])"))
    $uri = "https://dev.azure.com/$($env['ADO_ORG'])/$($env['ADO_PROJECT'])/_apis/wit/wiql?api-version=7.1"
    # ponytail: HttpClient direct (not Invoke-RestMethod) so the response body
    # isn't disposed before we can read it on error.
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $req = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::Post, $uri)
        $null = $req.Headers.TryAddWithoutValidation('Authorization', "Basic $cred")
        $req.Content = [System.Net.Http.StringContent]::new(
            '{"query":"SELECT [System.Id] FROM WorkItems"}',
            [System.Text.Encoding]::UTF8, 'application/json')
        $resp = $client.SendAsync($req).GetAwaiter().GetResult()
        if ($resp.IsSuccessStatusCode) {
            Test-Check 'ADO WIQL POST' $true "POST $uri"
        } else {
            $body = $resp.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            Test-Check 'ADO WIQL POST' $false "$([int]$resp.StatusCode) $($resp.ReasonPhrase)"
            if ($body.Length -gt 0) {
                Write-Host "         body: $($body.Substring(0, [Math]::Min(300, $body.Length)))"
            }
        }
    } finally {
        $client.Dispose()
    }
}

Write-Host ''
if ($failed) {
    Write-Host 'Preflight failed. Fix issues above before ./run.ps1.' -ForegroundColor Red
    exit 1
}
Write-Host 'All preflight checks passed.' -ForegroundColor Green