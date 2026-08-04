param(
    [Parameter(Mandatory = $true)]
    [string]$DatabaseUrl,

    [Parameter(Mandatory = $true)]
    [string]$GenerationApiBase,

    [Parameter(Mandatory = $true)]
    [string]$RendererCommandTemplate,

    [string]$AppEnv = "staging",
    [ValidateSet("torch", "onnx")]
    [string]$InferenceBackend = "torch",
    [Parameter(Mandatory = $true)]
    [string]$GenerationModelVersion,
    [string]$ActiveModelPath = "",
    [string]$GenerationModelS3Uri = "",
    [string]$GenerationModelSha256 = "",
    [string]$OnnxModelPath = "/models/checkpoints/best_model.onnx",
    [string]$CorsAllowOrigins = "",
    [string]$LogLevel = "INFO",
    [string]$SupabaseUrl = "<SUPABASE_URL>",
    [string]$SupabaseJwksUrl = "<SUPABASE_JWKS_URL>",
    [string]$SupabaseJwtAudience = "authenticated",
    [string]$SupabaseJwtIssuer = "<SUPABASE_JWT_ISSUER>",
    [string]$AwsS3Bucket = "<AWS_S3_BUCKET>",
    [string]$AwsRegion = "<AWS_REGION>",
    [string]$AwsAccessKeyId = "",
    [string]$AwsSecretAccessKey = "",
    [string]$GenerationAwsAccessKeyId = "",
    [string]$GenerationAwsSecretAccessKey = "",
    [string]$GenerationAwsRegion = "",
    [string]$GenerationAuthProviderNote = "",
    [string]$AwsS3SignedUrlTtlSec = "900",
    [string]$CalibrationRendererVersion = "unknown",
    [string]$CalibrationRenderMaxRetries = "3",
    [string]$CalibrationRenderWorkerPollSec = "2.0",
    [string]$CalibrationRenderCommandTimeoutSec = "300",
    [string]$CalibrationSamplePackVersion = "default",
    [string]$CalibrationRenderProfileId = "calibration_standard_v2",
    [string]$OutputDir = "scripts/render_payloads"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-DbFingerprint {
    param([Parameter(Mandatory = $true)][string]$Url)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Url)
        $hash = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }

    $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
    return $hex.Substring(0, 16)
}

function New-EnvVarSpec {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )

    return @{
        key = $Key
        value = $Value
    }
}

if (
    -not $RendererCommandTemplate.Contains("{input}") -or
    -not $RendererCommandTemplate.Contains("{output}")
) {
    throw "RendererCommandTemplate must contain both {input} and {output} placeholders."
}

if ($RendererCommandTemplate -match "backend\.workers\.calibration_render_worker") {
    throw "RendererCommandTemplate must be the per-job renderer subprocess command, not the worker startup command."
}

$hasMountedModel = -not [string]::IsNullOrWhiteSpace($ActiveModelPath)
$hasBootstrapModelUri = -not [string]::IsNullOrWhiteSpace($GenerationModelS3Uri)
$hasBootstrapModelHash = -not [string]::IsNullOrWhiteSpace($GenerationModelSha256)
$hasGenerationAccessKeyId = -not [string]::IsNullOrWhiteSpace($GenerationAwsAccessKeyId)
$hasGenerationSecretAccessKey = -not [string]::IsNullOrWhiteSpace($GenerationAwsSecretAccessKey)
$hasGenerationAuthProviderNote = -not [string]::IsNullOrWhiteSpace($GenerationAuthProviderNote)
$generationRegion = if (-not [string]::IsNullOrWhiteSpace($GenerationAwsRegion)) { $GenerationAwsRegion } else { $AwsRegion }

if (-not $hasMountedModel -and -not ($hasBootstrapModelUri -and $hasBootstrapModelHash)) {
    throw "Generation model configuration is incomplete. Provide ActiveModelPath (mounted model) OR both GenerationModelS3Uri and GenerationModelSha256 (bootstrap mode)."
}

if (($hasBootstrapModelUri -and -not $hasBootstrapModelHash) -or (-not $hasBootstrapModelUri -and $hasBootstrapModelHash)) {
    throw "Generation bootstrap mode requires both GenerationModelS3Uri and GenerationModelSha256."
}

if (($hasGenerationAccessKeyId -and -not $hasGenerationSecretAccessKey) -or (-not $hasGenerationAccessKeyId -and $hasGenerationSecretAccessKey)) {
    throw "Generation model-read credentials must include both GenerationAwsAccessKeyId and GenerationAwsSecretAccessKey."
}

if ($hasBootstrapModelUri -and $hasBootstrapModelHash) {
    if (-not ($hasGenerationAccessKeyId -and $hasGenerationSecretAccessKey) -and -not $hasGenerationAuthProviderNote) {
        throw "S3 bootstrap mode requires generation-specific AWS model-read credentials OR GenerationAuthProviderNote documenting an alternative credential provider."
    }
}

$workerDbFingerprint = Get-DbFingerprint -Url $DatabaseUrl

$includeAwsAccessKeyId = -not [string]::IsNullOrWhiteSpace($AwsAccessKeyId)
$includeAwsSecretAccessKey = -not [string]::IsNullOrWhiteSpace($AwsSecretAccessKey)

$generationVars = New-Object System.Collections.Generic.List[object]
$generationVars.Add((New-EnvVarSpec -Key "APP_ENV" -Value $AppEnv))
$generationVars.Add((New-EnvVarSpec -Key "LLM_STRICT_READINESS" -Value "true"))
$generationVars.Add((New-EnvVarSpec -Key "INFERENCE_BACKEND" -Value $InferenceBackend))
$generationVars.Add((New-EnvVarSpec -Key "DRUMTRACKAI_MODEL_VERSION" -Value $GenerationModelVersion))
if ($hasMountedModel) {
    $generationVars.Add((New-EnvVarSpec -Key "ACTIVE_MODEL_PATH" -Value $ActiveModelPath))
}
if ($InferenceBackend -eq "onnx") {
    $generationVars.Add((New-EnvVarSpec -Key "ONNX_MODEL_PATH" -Value $OnnxModelPath))
}
if ($hasBootstrapModelUri -and $hasBootstrapModelHash) {
    $generationVars.Add((New-EnvVarSpec -Key "GENERATION_MODEL_S3_URI" -Value $GenerationModelS3Uri))
    $generationVars.Add((New-EnvVarSpec -Key "GENERATION_MODEL_SHA256" -Value $GenerationModelSha256))
    $generationVars.Add((New-EnvVarSpec -Key "AWS_REGION" -Value $generationRegion))
    if ($hasGenerationAccessKeyId -and $hasGenerationSecretAccessKey) {
        $generationVars.Add((New-EnvVarSpec -Key "AWS_ACCESS_KEY_ID" -Value $GenerationAwsAccessKeyId))
        $generationVars.Add((New-EnvVarSpec -Key "AWS_SECRET_ACCESS_KEY" -Value $GenerationAwsSecretAccessKey))
    }
}
if (-not [string]::IsNullOrWhiteSpace($CorsAllowOrigins)) {
    $generationVars.Add((New-EnvVarSpec -Key "CORS_ALLOW_ORIGINS" -Value $CorsAllowOrigins))
}
if (-not [string]::IsNullOrWhiteSpace($LogLevel)) {
    $generationVars.Add((New-EnvVarSpec -Key "LOG_LEVEL" -Value $LogLevel))
}

$api2Vars = @(
    (New-EnvVarSpec -Key "APP_ENV" -Value $AppEnv),
    (New-EnvVarSpec -Key "CALIBRATION_V2_ENABLED" -Value "true"),
    (New-EnvVarSpec -Key "CALIBRATION_EXTERNAL_REVIEWERS_ENABLED" -Value "false"),
    (New-EnvVarSpec -Key "ALLOW_UNVERIFIED_JWT" -Value "false"),
    (New-EnvVarSpec -Key "DB_BACKEND" -Value "postgres"),
    (New-EnvVarSpec -Key "DATABASE_URL" -Value $DatabaseUrl),
    (New-EnvVarSpec -Key "CALIBRATION_GENERATION_MODE" -Value "http"),
    (New-EnvVarSpec -Key "CALIBRATION_GENERATION_API_BASE" -Value $GenerationApiBase),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_WORKER_DB_FINGERPRINT" -Value $workerDbFingerprint),
    (New-EnvVarSpec -Key "SUPABASE_URL" -Value $SupabaseUrl),
    (New-EnvVarSpec -Key "SUPABASE_JWKS_URL" -Value $SupabaseJwksUrl),
    (New-EnvVarSpec -Key "SUPABASE_JWT_AUDIENCE" -Value $SupabaseJwtAudience),
    (New-EnvVarSpec -Key "SUPABASE_JWT_ISSUER" -Value $SupabaseJwtIssuer),
    (New-EnvVarSpec -Key "AWS_S3_BUCKET" -Value $AwsS3Bucket),
    (New-EnvVarSpec -Key "AWS_REGION" -Value $AwsRegion),
    (New-EnvVarSpec -Key "AWS_S3_SIGNED_URL_TTL_SEC" -Value $AwsS3SignedUrlTtlSec)
)
if ($includeAwsAccessKeyId) {
    $api2Vars += (New-EnvVarSpec -Key "AWS_ACCESS_KEY_ID" -Value $AwsAccessKeyId)
}
if ($includeAwsSecretAccessKey) {
    $api2Vars += (New-EnvVarSpec -Key "AWS_SECRET_ACCESS_KEY" -Value $AwsSecretAccessKey)
}

$workerVars = @(
    (New-EnvVarSpec -Key "APP_ENV" -Value $AppEnv),
    (New-EnvVarSpec -Key "DB_BACKEND" -Value "postgres"),
    (New-EnvVarSpec -Key "DATABASE_URL" -Value $DatabaseUrl),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_WORKER_DB_FINGERPRINT" -Value $workerDbFingerprint),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_WORKER_COMMAND" -Value $RendererCommandTemplate),
    (New-EnvVarSpec -Key "CALIBRATION_RENDERER_VERSION" -Value $CalibrationRendererVersion),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_MAX_RETRIES" -Value $CalibrationRenderMaxRetries),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_WORKER_POLL_SEC" -Value $CalibrationRenderWorkerPollSec),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_COMMAND_TIMEOUT_SEC" -Value $CalibrationRenderCommandTimeoutSec),
    (New-EnvVarSpec -Key "AWS_S3_BUCKET" -Value $AwsS3Bucket),
    (New-EnvVarSpec -Key "AWS_REGION" -Value $AwsRegion),
    (New-EnvVarSpec -Key "CALIBRATION_SAMPLE_PACK_VERSION" -Value $CalibrationSamplePackVersion),
    (New-EnvVarSpec -Key "CALIBRATION_RENDER_PROFILE_ID" -Value $CalibrationRenderProfileId)
)
if ($includeAwsAccessKeyId) {
    $workerVars += (New-EnvVarSpec -Key "AWS_ACCESS_KEY_ID" -Value $AwsAccessKeyId)
}
if ($includeAwsSecretAccessKey) {
    $workerVars += (New-EnvVarSpec -Key "AWS_SECRET_ACCESS_KEY" -Value $AwsSecretAccessKey)
}

$outputPath = Resolve-Path "." | Select-Object -ExpandProperty Path
$payloadDir = Join-Path $outputPath $OutputDir
New-Item -ItemType Directory -Force -Path $payloadDir | Out-Null

$generationPayloadPath = Join-Path $payloadDir "generation_env_vars.json"
$api2PayloadPath = Join-Path $payloadDir "calibration_api2_env_vars.json"
$workerPayloadPath = Join-Path $payloadDir "calibration_worker_env_vars.json"
$summaryPath = Join-Path $payloadDir "strict_recovery_payload_summary.json"

$generationVars | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $generationPayloadPath
$api2Vars | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $api2PayloadPath
$workerVars | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $workerPayloadPath

$summary = @{
    app_env = $AppEnv
    inference_backend = $InferenceBackend
    worker_db_fingerprint = $workerDbFingerprint
    generation_payload = $generationPayloadPath
    api2_payload = $api2PayloadPath
    worker_payload = $workerPayloadPath
}

$summary | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $summaryPath

Write-Host "Generated strict recovery payloads:" -ForegroundColor Green
Write-Host "  $generationPayloadPath"
Write-Host "  $api2PayloadPath"
Write-Host "  $workerPayloadPath"
Write-Host "Worker DB fingerprint was computed and included in API2/worker payload files." -ForegroundColor Yellow
