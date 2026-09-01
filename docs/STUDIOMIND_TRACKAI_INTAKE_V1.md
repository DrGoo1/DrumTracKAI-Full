# StudioMind TrackAI intake contract v1.1

DrumTracKAI exposes a strict metadata-only compatibility boundary for the
StudioMind Full Production dispatch path:

- `GET /v1/studiomind/capabilities`
- `POST /v1/studiomind/generation-requests`

Both routes require a bearer value that is resolved at request time from
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
