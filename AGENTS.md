# DrumTracKAI agent operating contract

This file governs agents working anywhere in this repository. A nested
`AGENTS.md` may add stricter path-specific requirements but may not weaken these
rules.

## Product objective

Finish DrumTracKAI as a reliable drum-performance generation and calibration
system, then expose it to StudioMind through explicit versioned contracts.
Generation, personalization, rendering, artifact persistence, calibration, and
model readiness must be observable and reproducible.

## Required workflow

1. Read `docs/AI_ENGINEERING_CONTRACT.md`, the assigned issue or `.ai` task,
   relevant handoff/build-status documents, and the nearest tests.
2. Start from the task's declared base branch. The active integration line is
   currently `sync/render-main`; do not assume stale `main` is the correct base.
3. Implement one bounded change and avoid unrelated cleanup.
4. Add deterministic tests for success, failure, unavailable capability, and
   artifact/readiness behavior.
5. Run the exact task validation and the relevant backend/frontend/deployment
   gates.
6. Create or update `.ai/run-reports/<task-id>.json`.
7. Open a draft PR with exact evidence and local certification still required.

## Capability truth and fail-closed behavior

- Never return a successful personalized/generated result when a requested
  transformer, model, checkpoint, renderer, database, or artifact dependency
  was skipped.
- No broad exception handler may silently convert a production capability into
  an unchanged or cached fallback result.
- Report `capability_unavailable`, `model_not_ready`, `artifact_unavailable`, or
  another explicit failure/degraded state.
- Model manifests must include version, SHA-256, backend, input/output contract,
  readiness state, and source.
- Calibration artifacts must be durable and readable before a run is marked
  successful.
- Preserve seeds, source IDs, generation controls, model versions, event-stream
  hashes, render hashes, and judgment provenance.

## Data, privacy, and repository hygiene

Do not commit:

- private source audio or commercial reference audio;
- generated calibration libraries or large renders;
- model checkpoints or proprietary datasets unless explicitly approved;
- S3/cloud credentials, Render tokens, database secrets, signing identities, or
  plugin licenses;
- absolute workstation paths;
- temporary publish directories, build output, or local databases.

Use content hashes and approved artifact identifiers instead.

## PC and deployment boundaries

GPU, ONNX, checkpoint, Render deployment, Windows plugin, and listening
validation must run on the authorized PC or deployment service. Cloud agents may
implement deterministic code and fixtures but must report local/deployment
certification as pending.

## Cross-repository boundary

DrumTracKAI exposes versioned service/artifact contracts. StudioMind must not
import DrumTracKAI internal modules. Coordinate breaking contract changes with a
linked StudioMind task and compatibility fixtures.

## Completion report

Every substantial task reports:

- behavior changed;
- files changed;
- exact commands and results;
- model/checkpoint/artifact/deployment status;
- assumptions and unavailable local dependencies;
- migration and compatibility impact;
- branch, commit, run-report path, and PR.
