# DrumTracKAI repository instructions

Read `AGENTS.md`, `docs/AI_ENGINEERING_CONTRACT.md`, the assigned GitHub issue
or `.ai/tasks/<task-id>.json`, current handoff/build-status documents, and
relevant tests before editing.

Work from the task's declared base branch. The current integration/deployment
line is `sync/render-main` unless the task explicitly says otherwise.

Preserve these invariants:

- generation and readiness fail closed;
- no cached or unchanged fallback may masquerade as a successful personalized
  generation;
- model/checkpoint identity and SHA-256 are explicit;
- artifacts are durable and readable before success is reported;
- calibration judgments retain run, artifact, model, seed, and control
  provenance;
- private audio, checkpoints, licensed content, credentials, and absolute paths
  remain outside Git;
- StudioMind integration is through versioned contracts, never internal module
  imports.

For substantial work, create or update `.ai/run-reports/<task-id>.json` and
validate it with `python tools/agentctl.py`. Keep PRs draft until CI and required
PC/GPU/deployment/listening certification are complete.
