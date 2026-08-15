# Calibration V2 Strict Recovery Deployment Manifest

## Scope
This manifest captures strict deployment requirements for Calibration v2 fail-closed behavior.

## Required Service Deploy Order
1. Generation service (`llm_service`)
2. Calibration API2 service
3. Calibration render worker service

## Generation Service Required Environment
- `APP_ENV=staging` (or `production`)
- `LLM_STRICT_READINESS=true`
- `INFERENCE_BACKEND=torch` or `INFERENCE_BACKEND=onnx`
- `ACTIVE_MODEL_PATH` **or** `ACTIVE_MODEL_JSON`
- `ONNX_MODEL_PATH` (required when `INFERENCE_BACKEND=onnx`)

## Calibration API2 Required Environment
- `APP_ENV=staging` (or `production`)
- `DB_BACKEND=postgres`
- `DATABASE_URL=<canonical supabase postgres url>`
- `CALIBRATION_GENERATION_MODE=http`
- `CALIBRATION_GENERATION_API_BASE=<generation-service-base-url>`
- `ALLOW_UNVERIFIED_JWT=false` for production

## Render Worker Required Environment
- `APP_ENV=staging` (or `production`)
- `DB_BACKEND=postgres`
- `DATABASE_URL=<same canonical supabase postgres url as API2>`
- `CALIBRATION_RENDER_WORKER_DB_FINGERPRINT=<sha256(DATABASE_URL)[:16]>`
- `CALIBRATION_RENDER_WORKER_COMMAND=<real renderer command>`

## Strict Health/Readiness Checks
- Generation service `GET /healthz` returns backend/model checkpoint status.
- Generation service `GET /readyz` must return `200`.
- In strict env, inference requests must return `503` if no canonical backend is ready.

## Model Checkpoint Validation
Run:

```powershell
python scripts/strict_model_bootstrap.py
```

Expected:
- `ok: true`
- active checkpoint exists and size is above threshold
- onnx checkpoint valid if `INFERENCE_BACKEND=onnx`

## Strict Acceptance Requirements
- No fallback backend in production metadata.
- Queue-only rendering (no synthetic synchronous preview audio).
- Exactly three render jobs per trial (`neutral`, `control`, `challenger`).
- Trial creation fails closed with 502/dependency errors when generation is unavailable.
