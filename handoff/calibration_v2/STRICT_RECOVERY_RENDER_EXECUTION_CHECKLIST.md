# Strict Recovery Render Execution Checklist

## Scope
This checklist is for preparing strict recovery deployment payloads only.

Do not apply payloads yet while the generation checkpoint is unresolved.

## Hard deployment blockers
Deployment must not proceed until all conditions are true:
- A real generation checkpoint has been located.
- Checkpoint compatibility has been validated for the selected backend/runtime.
- Torch or ONNX Runtime is installed in the generation service image.
- Generation `GET /readyz` returns `200` with a non-`fallback` backend.

## Service command separation

### Generation service (llm_service)
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn llm_service.app:app --host 0.0.0.0 --port $PORT`
- Payload keys are generation-runtime keys only (no `DATABASE_URL` unless separately proven required by deployment design).

### Calibration API2 service
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn backend.calibration_api:app --host 0.0.0.0 --port $PORT` (or repo-specific API2 start command)
- Payload includes Postgres, strict calibration flags, generation API base, JWT/JWKS vars, and API-side worker DB fingerprint.
- API2 payload must not include `CALIBRATION_RENDER_WORKER_COMMAND`.

### Worker service
- Start command: `python -m backend.workers.calibration_render_worker`
- Payload includes queue/worker runtime keys and renderer subprocess template.

### Renderer subprocess command (executed by worker per job)
- Env var key: `CALIBRATION_RENDER_WORKER_COMMAND`
- Required shape: `<approved renderer binary/cmd> --input "{input}" --output "{output}"`
- Must contain both `{input}` and `{output}` placeholders.
- Must not be the worker startup command.
- Do not use synthetic preview/fallback renderer commands.

## Generate payload artifacts (local only)
From repo root, use placeholder values only:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/render_strict_recovery_payloads.ps1 `
  -DatabaseUrl "<DATABASE_URL>" `
  -GenerationApiBase "<GENERATION_API_BASE>" `
  -RendererCommandTemplate "<APPROVED_RENDERER_CMD> --input \"{input}\" --output \"{output}\"" `
  -GenerationModelVersion "<MODEL_VERSION>" `
  -ActiveModelPath "<ACTIVE_MODEL_PATH>" `
  -AppEnv "staging" `
  -InferenceBackend "torch" `
  -CorsAllowOrigins "<CORS_ORIGINS>" `
  -SupabaseUrl "<SUPABASE_URL>" `
  -SupabaseJwksUrl "<SUPABASE_JWKS_URL>" `
  -SupabaseJwtAudience "authenticated" `
  -SupabaseJwtIssuer "<SUPABASE_JWT_ISSUER>" `
  -AwsS3Bucket "<AWS_S3_BUCKET>" `
  -AwsRegion "<AWS_REGION>" `
  -AwsAccessKeyId "<AWS_ACCESS_KEY_ID>" `
  -AwsSecretAccessKey "<AWS_SECRET_ACCESS_KEY>"
```

Bootstrap-download model mode (instead of mounted path):
- Omit `-ActiveModelPath`
- Provide both:
  - `-GenerationModelS3Uri "<GENERATION_MODEL_S3_URI>"`
  - `-GenerationModelSha256 "<GENERATION_MODEL_SHA256>"`

## Expected payload outputs
- `scripts/render_payloads/generation_env_vars.json`
- `scripts/render_payloads/calibration_api2_env_vars.json`
- `scripts/render_payloads/calibration_worker_env_vars.json`

These are deployment artifacts and must remain untracked.

## Verification before any deployment action
- Confirm generation payload has no `DATABASE_URL`.
- Confirm API2 payload includes strict calibration flags + Postgres + Supabase JWT/JWKS keys.
- Confirm API2 payload does not include `CALIBRATION_RENDER_WORKER_COMMAND`.
- Confirm worker payload includes `CALIBRATION_RENDER_WORKER_COMMAND`, worker retry/poll/timeout keys, and renderer version key.

## Security reminders
- Keep secrets in environment variables/secret stores only.
- Do not commit real credential values, JWTs, or full connection strings.
