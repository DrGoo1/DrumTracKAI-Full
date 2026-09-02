# StudioMind TrackAI intake contract v1.1

DrumTracKAI exposes a strict metadata-only compatibility boundary for the
StudioMind Full Production dispatch path:

- `GET /v1/studiomind/capabilities`
- `POST /v1/studiomind/generation-requests`
- `POST /v1/studiomind/generation-plans`
- `POST /v1/studiomind/generation-executions`

All four routes require a bearer value that is resolved at request time from
`STUDIOMIND_TRACKAI_SANDBOX_AUTH`. The value is not stored in source, request
models, receipts, or logs.

The POST route accepts the StudioMind generation request plus an explicit
`production_role`. Version 1.1 requires this role because a provider must not
infer instrument identity from a free-form role key. DrumTracKAI accepts only
`drums` and `percussion`, and only `midi` or `performance_description`
artifacts. It independently recomputes the canonical SHA-256 payload digest,
rejects named-artist targets, and requires the no-imitation and
no-automatic-submission invariants.

A successful HTTP 202 response is a deterministic metadata-validation receipt.
It explicitly reports `validated_metadata_only`, `generation_authorized=false`,
and `artifact_ready=false`. No performance has been generated or queued. This
keeps the first cross-repository compatibility test separate from the later
production-engine, artifact, and candidate-ingestion gates.

This v1.1 envelope is intended to become a shared TrackAI contract. Future
Bass, Guitar, Keys, Horns, and Vocal products should reuse the envelope and
receipt schemas while narrowing their own accepted `production_role` and
artifact capabilities.

## Isolated loopback host

`backend.studiomind_trackai_contract_app:app` is a deliberately minimal ASGI
host for cross-repository certification. It mounts only `/healthz`, the
authenticated capability and metadata-intake routes, and authenticated
non-executing plan preparation.
It does not import the calibration application, database, production model,
render worker, artifact service, or DAW integration. A successful loopback
certification therefore proves protocol compatibility only; it cannot be read
as generation, deployment, artifact, or musical-quality evidence.

## Generation-plan preparation

The generation-plan route is the next bounded provider layer. It accepts the
already validated v1.1 envelope and binds it to:

- exact ordered song sections with bar counts and energy;
- one tempo and time signature within the reviewed request constraints;
- a mandatory deterministic seed;
- the exact parent-artifact hashes from the StudioMind request; and
- a digest-bound commercial-rights manifest that forbids performer-identity
  and named-artist generation targets.

The provider independently recomputes the validation job ID, rights-manifest
digest, and generation-plan digest. Arrangement identity, section order,
tempo, or source-rights drift fails closed.

A successful plan response says `prepared_for_human_review`. It includes the
sanitized DrumTracKAI provider payload and a stable plan digest, but retains:

- `generation_authorized=false`;
- `artifact_ready=false`;
- `candidate_commit_authorized=false`; and
- `automatic_retry_authorized=false`.

No model, renderer, calibration record, artifact store, StudioMind project, or
DAW is touched by plan preparation. A later execution task must require a
short-lived human approval bound to the exact plan digest, a durable replay
guard, the certified production model, and a separately reviewed artifact
lifecycle before it may invoke generation.

## One-time candidate execution

The execution route is present but fails closed unless all of the following
are explicitly configured:

- `STUDIOMIND_TRACKAI_GENERATION_ENABLED=true`;
- `DRUMTRACKAI_GENERATION_API_BASE` points to loopback HTTP or HTTPS; and
- `STUDIOMIND_TRACKAI_REPLAY_DB_PATH` is an absolute path whose parent exists.

Execution requires a digest-valid approval receipt with a maximum lifetime of
15 minutes. The approval must identify the exact plan ID and digest, state that
human review occurred, prohibit automatic execution, and be single-use. Before
the production endpoint is called, its approval ID, approval digest, and plan
digest are atomically consumed in SQLite. Failed provider calls therefore do
not become implicit retries; a new deterministic plan and reviewed approval
are required.

The existing DrumTracKAI `/v1/generate-drums` route receives only a bounded,
deterministically compiled payload. The returned artifact must be valid base64,
begin with a Standard MIDI File header, and remain within the configured size
limit. Only allowlisted provider metadata is returned.

A successful response creates a candidate for listening review. It does not
authorize candidate commitment, automatic retry, DAW execution, or acceptance
of the musical result. Audio rendering and durable candidate storage remain
separate later gates.
