# Calibration v2 — Assimilation-Driven Runtime Runbook

## Objective

Make `/calibration` operate from the drummer personality extracted by the
DrumTrackAI Admin app from analyzed signature-song drum stems:

1. Admin workstation ingests processed stem folders and runs Phases 2-7 and
   32-42 into the canonical Calibration v2 Postgres database.
2. Calibration v2 lists only assimilation-ready drummer models.
3. A verified reviewer selects a drummer.
4. API2 reuses an open trial or queues a new blinded control/challenger trial.
5. Control and challenger use the same base groove and paired seed, but the
   challenger applies the active Phase 6-derived treatment.
6. The render worker renders neutral/A/B through the same neutral playback kit.
7. The reviewer page polls until all strict render evidence is ready.

The source-song drum stems are analysis inputs. They are not public playback
artifacts and are not used as candidate audio samples.

---

## Required security posture

Keep these values throughout staging validation:

```text
ALLOW_UNVERIFIED_JWT=false
CALIBRATION_INTERNAL_REVIEWERS_ENABLED=true
CALIBRATION_EXTERNAL_REVIEWERS_ENABLED=false
```

Do not reuse the failed diagnostic trials:

```text
trial_bbe9c944ff124ec8
trial_eba778831aac48a5
```

---

## Phase 1 — Publish Admin-app assimilation from the workstation

Run this on the Windows/Mac workstation that actually contains the processed
stems. Do not run it inside Render with a placeholder local path.

The expected local directory layout is:

```text
<processed-stems-root>/
  john_bonham/
    song_01/
      drum_analysis.json
      ...processed drum stems...
    song_02/
      drum_analysis.json
      ...processed drum stems...
```

### Windows PowerShell

```powershell
cd F:\DrumTracKAI_v1.1.17

git fetch full sync/render-main
git checkout sync/render-main
git pull --ff-only full sync/render-main

# Activate the repository environment first.
# Never commit or paste this URL into logs or reports.
$env:DB_BACKEND = "postgres"
$env:DATABASE_URL = "<CANONICAL_SUPABASE_POSTGRES_URL>"

python scripts/publish_admin_assimilation_to_postgres.py `
  --base-dir "F:\path\to\processed_stems" `
  --drummer john_bonham `
  --max-events-per-stem 5000 `
  --report "handoff\calibration_v2\john_bonham_publish.local.json"
```

The report filename is local-only. Do not commit it if it contains operational
identifiers.

Required final marker:

```text
ADMIN_ASSIMILATION_PUBLISH_PASS
```

Required evidence in the sanitized report:

```text
model_ready=true
profile_snapshot_hash=<non-empty SHA-256>
source_song_count>0
source_stem_count>0
hit_event_count>0
rollup_version=<non-empty>
phase5.saved=true
phase6.saved=true
```

The publisher runs the existing Admin pipeline against Postgres:

```text
ingest_processed_stems_song_folder
Phase 2 hit extraction
Phase 3 fills/techniques
Phase 4 microtiming/dynamics
Phase 5 rollup
Phase 6 persona preset
Phase 7 assimilation profiles
Phase 32-42 personality features
```

### Important Render correction

The old API2 environment value:

```text
ASSIMILATION_AUTO_POPULATE_BASE_DIR=/absolute/path/to/processed_stems
```

cannot point to files on the Admin workstation. Set:

```text
ASSIMILATION_AUTO_POPULATE_ON_STARTUP=false
```

The local publisher above is now the supported bridge for workstation-resident
processed stems.

---

## Phase 2 — Verify the cloud assimilation model

Open a shell in `drumtrackai-calibration-api2` after the Postgres publication.

```bash
python scripts/verify_calibration_assimilation_path.py \
  --drummer john_bonham
```

Required final marker:

```text
CALIBRATION_ASSIMILATION_PATH_PASS
```

The report must show:

```text
profile_resolved=true
model_ready=true
profile_snapshot_hash=<non-empty>
source_song_count>0
hit_event_count>0
profile_sections.microtiming=true
profile_sections.dynamics=true
```

Additional profile sections may be present depending on the completed Admin
phases.

---

## Phase 3 — Bootstrap the Phase 6 calibration treatment

The reviewer compares:

```text
Control    = complete current assimilated drummer profile
Challenger = same profile plus the active bounded Phase 6 preset deltas
```

Use a real Supabase admin UUID as `created_by`.

```bash
python scripts/verify_calibration_assimilation_path.py \
  --drummer john_bonham \
  --bootstrap-treatment \
  --created-by "<SUPABASE_ADMIN_USER_UUID>"
```

Alternatively call, with a verified admin JWT:

```http
POST /calibration/v2/admin/drummers/john_bonham/bootstrap-treatment
```

Then verify:

```http
GET /calibration/v2/admin/drummers/john_bonham/assimilation
```

Required:

```text
model_ready=true
can_queue_trial=true
active_treatment_id=<non-empty>
```

---

## Phase 4 — Prove the assimilated profile drives production generation

From the API2 Render shell:

```bash
python scripts/verify_calibration_assimilation_path.py \
  --drummer john_bonham \
  --probe-generation \
  --base-groove-id base_groove \
  --seed 130742 \
  --repeats 4
```

Required evidence:

```text
control_backend=onnx
challenger_backend=onnx
control_profile_snapshot_hash=<non-empty>
challenger_profile_snapshot_hash=<non-empty>
control_rollup_version=<non-empty>
challenger_rollup_version=<non-empty>
control/challenger base_pattern_hash match
control/challenger paired_seed match
control_event_stream_hash != challenger_event_stream_hash
control_event_count > 0
challenger_event_count > 0
```

Do not continue if either backend is `fallback`. The historical strict failure
was caused by fallback generation with null model pointers.

---

## Phase 5 — Configure API2 for assimilation-driven reviewer trials

Required API2 values:

```text
APP_ENV=staging
CALIBRATION_V2_ENABLED=true
CALIBRATION_INTERNAL_REVIEWERS_ENABLED=true
CALIBRATION_EXTERNAL_REVIEWERS_ENABLED=false
CALIBRATION_AUTO_QUEUE_REVIEW_TRIALS=true
ALLOW_UNVERIFIED_JWT=false

DB_BACKEND=postgres
DATABASE_URL=<CANONICAL_SUPABASE_POSTGRES_URL>

CALIBRATION_GENERATION_MODE=http
CALIBRATION_GENERATION_API_BASE=http://drumtrackai-generation-api:10000

ASSIMILATION_AUTO_POPULATE_ON_STARTUP=false
CALIBRATION_MIN_ASSIMILATED_SONGS=1
CALIBRATION_MIN_HIT_EVENTS=1
CALIBRATION_MIN_PROFILE_SECTIONS=2

CALIBRATION_DEFAULT_BASE_GROOVE_ID=base_groove
CALIBRATION_DEFAULT_REPEATS=4
CALIBRATION_REVIEW_POLL_SECONDS=5

CALIBRATION_RENDER_PROFILE_ID=calibration_standard_v2
CALIBRATION_RENDERER_VERSION=<APPROVED_NEUTRAL_RENDERER_VERSION>
CALIBRATION_SAMPLE_PACK_VERSION=<APPROVED_NEUTRAL_PLAYBACK_KIT_VERSION>
CALIBRATION_KIT_ID=<APPROVED_NEUTRAL_PLAYBACK_KIT_ID>
```

The neutral playback kit is only the audition instrument. The drummer identity
comes from the Admin assimilation profile and Phase 6 treatment.

API2 must not contain:

```text
CALIBRATION_RENDER_WORKER_COMMAND
```

---

## Phase 6 — Configure the render worker

The worker must render all three lanes through the same playback chain.

```text
APP_ENV=staging
DB_BACKEND=postgres
DATABASE_URL=<SAME_CANONICAL_DATABASE>
CALIBRATION_RENDER_WORKER_DB_FINGERPRINT=<MATCHING_FINGERPRINT>

CALIBRATION_RENDER_WORKER_COMMAND=python scripts/calibration_sample_renderer.py --input "{input}" --output "{output}"
CALIBRATION_RENDERER_VERSION=<APPROVED_NEUTRAL_RENDERER_VERSION>
CALIBRATION_ALLOWED_RENDERERS=<SAME_VERSION>
CALIBRATION_SAMPLE_PACK_VERSION=<APPROVED_NEUTRAL_PLAYBACK_KIT_VERSION>
CALIBRATION_ALLOWED_SAMPLE_PACKS=<SAME_VERSION>
CALIBRATION_SAMPLE_MANIFEST_URI=s3://<BUCKET>/<NEUTRAL_KIT>/kit_manifest_v1.json
CALIBRATION_RENDER_UPLOAD_ENABLED=true
CALIBRATION_RENDER_OUTPUT_PREFIX=calibration/v2
```

Do not configure `calibration_procedural_renderer.py` for staging reviewer
trials. Strict readiness rejects procedural or local-only artifacts.

---

## Phase 7 — Deploy one exact commit

Deploy the same `sync/render-main` head to:

```text
drumtrackai-generation-api
drumtrackai-calibration-api2
drumtrackai-calibration-render-worker
Netlify reviewer frontend
```

Deploy order:

```text
1. generation service
2. API2
3. render worker
4. Netlify frontend
```

Before creating a trial, verify generation internally:

```text
GET http://drumtrackai-generation-api:10000/readyz
```

Required:

```text
HTTP 200
ready=true
backend=onnx
backend_ready=true
model_sha256_verified=true
```

---

## Phase 8 — Provision one secure internal reviewer

The Supabase user must have:

```text
active reviewer_profiles row
auth_user_id=<Supabase user UUID>
consented_at=<non-null>
is_active=true
app_user_roles role=internal_reviewer
```

Do not use `ALLOW_UNVERIFIED_JWT=true`.

---

## Phase 9 — Browser workflow

1. Open `/calibration`.
2. Sign in with the Supabase magic link.
3. Confirm the drummer selector lists assimilation-ready models from the Admin
   database and shows analyzed-song count/assimilation score.
4. Select John Bonham.
5. The page should either load an existing ready comparison or display
   `Preparing a new blinded comparison`.
6. The page polls `/calibration/v2/reviewer/next` until strict readiness passes.
7. Play neutral, A and B.
8. Submit one review.

The reviewer response must not expose control/challenger mapping, treatment ID,
profile snapshots or model internals.

---

## Phase 10 — Strict runtime evidence

From API2 shell:

```bash
python scripts/verify_calibration_v2_runtime.py \
  --trial-id "<NEW_TRIAL_ID>" \
  --wait-seconds 300 \
  --poll-seconds 5
```

Required final marker:

```text
CALIBRATION_V2_STRICT_RUNTIME_PASS
```

Required evidence:

```text
control_backend=onnx
challenger_backend=onnx
3 non-empty event streams
exactly 3 completed render jobs
3 durable non-procedural artifacts
S3/HTTPS storage
artifact SHA values present
```

Submit the same review twice with the same `Idempotency-Key`. Verify that only
one `pairwise_judgments` row exists.

---

## Release posture

Keep external reviewers disabled until the entire runbook passes for a fresh
trial. Historical diagnostic/fallback trials remain excluded from calibration
learning and exports.
