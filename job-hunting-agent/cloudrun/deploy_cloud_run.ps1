param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectId,

    [string]$Region = "us-central1",
    [string]$Repository = "jobs-agent",
    [string]$ImageName = "job-hunting-agent",
    [string]$Network = "jobs-agent-net",
    [string]$Subnet = "jobs-agent-run-subnet",
    [string]$SubnetRange = "10.80.1.0/24",
    [string]$Router = "jobs-agent-router",
    [string]$Nat = "jobs-agent-nat",
    [string]$EgressIpName = "jobs-agent-egress-ip",

    [switch]$BootstrapSecrets,
    [switch]$SkipAtlasPrompt,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

function Ensure-GcloudOnPath {
    if (Get-Command gcloud -ErrorAction SilentlyContinue) {
        return
    }
    $candidates = @(
        "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
        "C:\Program Files (x86)\Google\Cloud SDK\google-cloud-sdk\bin",
        "C:\Program Files\Google\Cloud SDK\google-cloud-sdk\bin"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate "gcloud.cmd")) {
            $env:Path = "$candidate;$env:Path"
            return
        }
    }
}

function Invoke-Gcloud {
    param([string[]]$Arguments)
    Write-Host ("gcloud " + ($Arguments -join " "))
    & gcloud @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud command failed: $($Arguments -join ' ')"
    }
}

function Get-GcloudValue {
    param([string[]]$Arguments)
    $value = & gcloud @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud command failed: $($Arguments -join ' ')"
    }
    return ($value | Out-String).Trim()
}

function Test-GcloudResource {
    param([string[]]$Arguments)
    try {
        & gcloud @Arguments *> $null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Load-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $parts = $line.Split("=", 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim('"').Trim("'")
        if (-not [Environment]::GetEnvironmentVariable($key, "Process")) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

function Get-FirstEnvValue {
    param([string[]]$Names)
    foreach ($name in $Names) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrWhiteSpace($value)) {
            return $value
        }
    }
    return $null
}

function Test-Secret {
    param([string]$SecretId)
    return Test-GcloudResource @("secrets", "describe", $SecretId, "--project", $ProjectId)
}

function Ensure-SecretFromEnv {
    param(
        [string]$SecretId,
        [string[]]$EnvNames,
        [bool]$Required = $false
    )
    $value = Get-FirstEnvValue $EnvNames
    if ([string]::IsNullOrWhiteSpace($value)) {
        if ($Required) {
            throw "Missing required environment variable for secret '$SecretId': $($EnvNames -join ' or ')"
        }
        Write-Host "Skipping secret '$SecretId'; no local env value found for $($EnvNames -join ' or ')."
        return
    }
    if (-not (Test-Secret $SecretId)) {
        Invoke-Gcloud @("secrets", "create", $SecretId, "--replication-policy", "automatic", "--project", $ProjectId)
    }
    $tmp = [System.IO.Path]::GetTempFileName()
    try {
        [System.IO.File]::WriteAllText($tmp, $value)
        Invoke-Gcloud @("secrets", "versions", "add", $SecretId, "--data-file", $tmp, "--project", $ProjectId)
    }
    finally {
        if (Test-Path -LiteralPath $tmp) {
            Remove-Item -LiteralPath $tmp -Force
        }
    }
}

function Assert-Secret {
    param([string]$SecretId)
    if (-not (Test-Secret $SecretId)) {
        throw "Secret '$SecretId' does not exist. Re-run with -BootstrapSecrets after setting local env vars, or create it in Secret Manager."
    }
}

function Add-SecretBindingIfExists {
    param(
        [hashtable]$Bindings,
        [string]$EnvName,
        [string]$SecretId
    )
    if (Test-Secret $SecretId) {
        $Bindings[$EnvName] = "$SecretId`:latest"
    }
}

function Join-Map {
    param([hashtable]$Map)
    if (-not $Map -or $Map.Count -eq 0) {
        return $null
    }
    return (($Map.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ",")
}

function Ensure-CloudNetwork {
    if (-not (Test-GcloudResource @("compute", "networks", "describe", $Network, "--project", $ProjectId))) {
        Invoke-Gcloud @("compute", "networks", "create", $Network, "--subnet-mode", "custom", "--project", $ProjectId)
    }
    if (-not (Test-GcloudResource @("compute", "networks", "subnets", "describe", $Subnet, "--region", $Region, "--project", $ProjectId))) {
        Invoke-Gcloud @("compute", "networks", "subnets", "create", $Subnet, "--network", $Network, "--range", $SubnetRange, "--region", $Region, "--project", $ProjectId)
    }
    if (-not (Test-GcloudResource @("compute", "addresses", "describe", $EgressIpName, "--region", $Region, "--project", $ProjectId))) {
        Invoke-Gcloud @("compute", "addresses", "create", $EgressIpName, "--region", $Region, "--project", $ProjectId)
    }
    if (-not (Test-GcloudResource @("compute", "routers", "describe", $Router, "--region", $Region, "--project", $ProjectId))) {
        Invoke-Gcloud @("compute", "routers", "create", $Router, "--network", $Network, "--region", $Region, "--project", $ProjectId)
    }
    if (-not (Test-GcloudResource @("compute", "routers", "nats", "describe", $Nat, "--router", $Router, "--region", $Region, "--project", $ProjectId))) {
        Invoke-Gcloud @(
            "compute", "routers", "nats", "create", $Nat,
            "--router", $Router,
            "--region", $Region,
            "--nat-custom-subnet-ip-ranges", $Subnet,
            "--nat-external-ip-pool", $EgressIpName,
            "--project", $ProjectId
        )
    }
    else {
        Invoke-Gcloud @(
            "compute", "routers", "nats", "update", $Nat,
            "--router", $Router,
            "--region", $Region,
            "--nat-custom-subnet-ip-ranges", $Subnet,
            "--nat-external-ip-pool", $EgressIpName,
            "--project", $ProjectId
        )
    }
}

function Deploy-Service {
    param(
        [string]$Name,
        [string]$Command,
        [string[]]$ArgsList,
        [hashtable]$EnvVars,
        [hashtable]$Secrets,
        [string]$Memory = "1Gi",
        [string]$Cpu = "1",
        [string]$Timeout = "300",
        [string]$Concurrency = "20",
        [string]$MinInstances = "0",
        [string]$MaxInstances = "3"
    )
    $deployArgs = @(
        "run", "deploy", $Name,
        "--image", $script:Image,
        "--region", $Region,
        "--platform", "managed",
        "--allow-unauthenticated",
        "--port", "8080",
        "--command", $Command,
        "--memory", $Memory,
        "--cpu", $Cpu,
        "--timeout", $Timeout,
        "--concurrency", $Concurrency,
        "--min-instances", $MinInstances,
        "--max-instances", $MaxInstances,
        "--network", $Network,
        "--subnet", $Subnet,
        "--vpc-egress", "all-traffic",
        "--project", $ProjectId
    )
    if ($ArgsList -and $ArgsList.Count -gt 0) {
        $deployArgs += @("--args", ($ArgsList -join ","))
    }
    $envString = Join-Map $EnvVars
    if ($envString) {
        $deployArgs += @("--set-env-vars", $envString)
    }
    $secretString = Join-Map $Secrets
    if ($secretString) {
        $deployArgs += @("--set-secrets", $secretString)
    }
    Invoke-Gcloud $deployArgs
}

Ensure-GcloudOnPath
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud CLI is not installed or not on PATH."
}

Invoke-Gcloud @("config", "set", "project", $ProjectId)
Invoke-Gcloud @(
    "services", "enable",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "compute.googleapis.com",
    "vpcaccess.googleapis.com",
    "--project", $ProjectId
)

$projectNumber = Get-GcloudValue @("projects", "describe", $ProjectId, "--format", "value(projectNumber)")
$runServiceAccount = "$projectNumber-compute@developer.gserviceaccount.com"
Invoke-Gcloud @(
    "projects", "add-iam-policy-binding", $ProjectId,
    "--member", "serviceAccount:$runServiceAccount",
    "--role", "roles/secretmanager.secretAccessor"
)

Load-DotEnv (Join-Path $PSScriptRoot "..\.env")
Load-DotEnv (Join-Path $PSScriptRoot "..\.env.local")
Load-DotEnv (Join-Path $PSScriptRoot "..\..\.env")
Load-DotEnv (Join-Path $PSScriptRoot "..\..\.env.local")

if ($BootstrapSecrets) {
    Ensure-SecretFromEnv "mongo-uri" @("MONGO_URI") $true
    Ensure-SecretFromEnv "gemini-api-key" @("GEMINI_API_KEY", "GOOGLE_API_KEY") $false
    Ensure-SecretFromEnv "elastic-url" @("ELASTIC_URL") $false
    Ensure-SecretFromEnv "elastic-api-key" @("ELASTIC_API_KEY") $false
    Ensure-SecretFromEnv "elastic-cloud-id" @("ELASTIC_CLOUD_ID") $false
    Ensure-SecretFromEnv "phoenix-api-key" @("PHOENIX_API_KEY") $false
    Ensure-SecretFromEnv "phoenix-collector-endpoint" @("PHOENIX_COLLECTOR_ENDPOINT") $false
    Ensure-SecretFromEnv "phoenix-project-name" @("PHOENIX_PROJECT_NAME") $false
    Ensure-SecretFromEnv "email-imap-server" @("EMAIL_IMAP_SERVER") $false
    Ensure-SecretFromEnv "email-username" @("EMAIL_USERNAME") $false
    Ensure-SecretFromEnv "email-password" @("EMAIL_PASSWORD") $false
    Ensure-SecretFromEnv "gmail-oauth-client-id" @("GMAIL_OAUTH_CLIENT_ID") $false
    Ensure-SecretFromEnv "gmail-oauth-client-secret" @("GMAIL_OAUTH_CLIENT_SECRET") $false
    Ensure-SecretFromEnv "ats-account-password" @("ATS_ACCOUNT_PASSWORD", "APPLICATION_ACCOUNT_PASSWORD") $false
    Ensure-SecretFromEnv "app-access-password" @("APP_ACCESS_PASSWORD") $false
}

Assert-Secret "mongo-uri"

Ensure-CloudNetwork
$egressIp = Get-GcloudValue @("compute", "addresses", "describe", $EgressIpName, "--region", $Region, "--project", $ProjectId, "--format", "value(address)")
Write-Host ""
Write-Host "Static outbound IP for MongoDB Atlas allowlist: $egressIp/32"
Write-Host ""
if (-not $SkipAtlasPrompt) {
    Read-Host "Add $egressIp/32 to MongoDB Atlas Network Access, then press Enter to continue deployment"
}

if ($SkipBuild) {
    Write-Host "Skipping Artifact Registry repository creation check because -SkipBuild was passed."
}
elseif (-not (Test-GcloudResource @("artifacts", "repositories", "describe", $Repository, "--location", $Region, "--project", $ProjectId))) {
    try {
        Invoke-Gcloud @(
            "artifacts", "repositories", "create", $Repository,
            "--repository-format", "docker",
            "--location", $Region,
            "--description", "Job hunting agent images",
            "--project", $ProjectId
        )
    }
    catch {
        Write-Host "Artifact Registry repository '$Repository' was not created. It may already exist; continuing."
    }
}

$script:Image = "$Region-docker.pkg.dev/$ProjectId/$Repository/$ImageName`:latest"
if (-not $SkipBuild) {
    Invoke-Gcloud @("builds", "submit", "--tag", $script:Image, ".", "--project", $ProjectId)
}
else {
    Write-Host "Skipping Cloud Build; using existing image $script:Image"
}

$mongoSecrets = @{"MONGO_URI" = "mongo-uri:latest"}
Deploy-Service "jobs-mongo" "python" @("mcp_servers/mongo_server.py") @{"MONGO_DB_NAME" = "jobs_hunter"} $mongoSecrets "1Gi" "1" "300" "20" "0" "3"
$mongoUrl = Get-GcloudValue @("run", "services", "describe", "jobs-mongo", "--region", $Region, "--project", $ProjectId, "--format", "value(status.url)")

$arizeSecrets = @{}
Add-SecretBindingIfExists $arizeSecrets "GEMINI_API_KEY" "gemini-api-key"
Add-SecretBindingIfExists $arizeSecrets "PHOENIX_API_KEY" "phoenix-api-key"
Add-SecretBindingIfExists $arizeSecrets "PHOENIX_COLLECTOR_ENDPOINT" "phoenix-collector-endpoint"
Add-SecretBindingIfExists $arizeSecrets "PHOENIX_PROJECT_NAME" "phoenix-project-name"
Deploy-Service "jobs-arize" "python" @("mcp_servers/arize_server.py") @{} $arizeSecrets "1Gi" "1" "300" "20" "0" "3"
$arizeUrl = Get-GcloudValue @("run", "services", "describe", "jobs-arize", "--region", $Region, "--project", $ProjectId, "--format", "value(status.url)")

$elasticSecrets = @{}
Add-SecretBindingIfExists $elasticSecrets "ELASTIC_URL" "elastic-url"
Add-SecretBindingIfExists $elasticSecrets "ELASTIC_API_KEY" "elastic-api-key"
Add-SecretBindingIfExists $elasticSecrets "ELASTIC_CLOUD_ID" "elastic-cloud-id"
Deploy-Service "jobs-elastic" "python" @("mcp_servers/elastic_server.py") @{"MONGO_URL" = $mongoUrl; "ARIZE_URL" = $arizeUrl} $elasticSecrets "1Gi" "1" "300" "20" "0" "3"
$elasticUrl = Get-GcloudValue @("run", "services", "describe", "jobs-elastic", "--region", $Region, "--project", $ProjectId, "--format", "value(status.url)")

$emailSecrets = @{}
Add-SecretBindingIfExists $emailSecrets "EMAIL_IMAP_SERVER" "email-imap-server"
Add-SecretBindingIfExists $emailSecrets "EMAIL_USERNAME" "email-username"
Add-SecretBindingIfExists $emailSecrets "EMAIL_PASSWORD" "email-password"
Add-SecretBindingIfExists $emailSecrets "GEMINI_API_KEY" "gemini-api-key"
Add-SecretBindingIfExists $emailSecrets "GMAIL_OAUTH_CLIENT_ID" "gmail-oauth-client-id"
Add-SecretBindingIfExists $emailSecrets "GMAIL_OAUTH_CLIENT_SECRET" "gmail-oauth-client-secret"
Deploy-Service "jobs-email" "python" @("mcp_servers/email_server.py") @{"MONGO_URL" = $mongoUrl; "ELASTIC_URL_API" = $elasticUrl} $emailSecrets "1Gi" "1" "300" "10" "0" "2"
$emailUrl = Get-GcloudValue @("run", "services", "describe", "jobs-email", "--region", $Region, "--project", $ProjectId, "--format", "value(status.url)")

$playwrightSecrets = @{}
Add-SecretBindingIfExists $playwrightSecrets "GEMINI_API_KEY" "gemini-api-key"
Add-SecretBindingIfExists $playwrightSecrets "ATS_ACCOUNT_PASSWORD" "ats-account-password"
Deploy-Service "jobs-playwright" "python" @("mcp_servers/playwright_server.py") @{"PLAYWRIGHT_HEADLESS" = "true"; "EMAIL_URL" = $emailUrl; "EMAIL_SERVER_PORT" = "8080"} $playwrightSecrets "2Gi" "2" "3600" "1" "0" "1"
$playwrightUrl = Get-GcloudValue @("run", "services", "describe", "jobs-playwright", "--region", $Region, "--project", $ProjectId, "--format", "value(status.url)")

Invoke-Gcloud @(
    "run", "services", "update", "jobs-elastic",
    "--region", $Region,
    "--project", $ProjectId,
    "--update-env-vars", "PLAYWRIGHT_URL=$playwrightUrl"
)

$frontendSecrets = @{}
Add-SecretBindingIfExists $frontendSecrets "GEMINI_API_KEY" "gemini-api-key"
Add-SecretBindingIfExists $frontendSecrets "ELASTIC_API_KEY" "elastic-api-key"
Add-SecretBindingIfExists $frontendSecrets "PHOENIX_API_KEY" "phoenix-api-key"
Add-SecretBindingIfExists $frontendSecrets "APP_ACCESS_PASSWORD" "app-access-password"
Add-SecretBindingIfExists $frontendSecrets "GMAIL_OAUTH_CLIENT_ID" "gmail-oauth-client-id"
Add-SecretBindingIfExists $frontendSecrets "GMAIL_OAUTH_CLIENT_SECRET" "gmail-oauth-client-secret"
Deploy-Service "jobs-frontend" "python" @("cloudrun/run_streamlit.py") @{
    "MONGO_URL" = $mongoUrl
    "ELASTIC_URL_API" = $elasticUrl
    "ARIZE_URL" = $arizeUrl
    "EMAIL_URL" = $emailUrl
    "PLAYWRIGHT_URL" = $playwrightUrl
    "DEMO_LOCKED_EMAIL" = "demo@example.com"
    "DEMO_EMAIL_OAUTH_LOCKED" = "true"
} $frontendSecrets "1Gi" "1" "900" "20" "1" "1"
$frontendUrl = Get-GcloudValue @("run", "services", "describe", "jobs-frontend", "--region", $Region, "--project", $ProjectId, "--format", "value(status.url)")

Invoke-Gcloud @(
    "run", "services", "update", "jobs-frontend",
    "--region", $Region,
    "--project", $ProjectId,
    "--update-env-vars", "APP_BASE_URL=$frontendUrl"
)

Write-Host ""
Write-Host "Cloud Run deployment complete."
Write-Host "Frontend: $frontendUrl"
Write-Host "Mongo health: $mongoUrl/health"
Write-Host "Elastic health: $elasticUrl/health"
Write-Host "Arize health: $arizeUrl/health"
Write-Host "Atlas allowlist IP: $egressIp/32"
