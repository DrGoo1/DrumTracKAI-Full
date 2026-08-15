---
name: issue-implementation
description: Implement one bounded GitHub issue or .ai task, validate it, and prepare a draft pull request with machine-readable evidence.
---

Read `AGENTS.md`, `docs/AI_ENGINEERING_CONTRACT.md`, the assigned GitHub issue
or `.ai/tasks/<task-id>.json`, and the nearest path-specific instructions before
editing.

Work on one focused task branch. Do not expand scope, make unrelated cleanup, or
invent local/DAW/model results. Run the required validation, create or update
`.ai/run-reports/<task-id>.json`, validate it with `tools/agentctl.py`, and keep
the pull request in draft while evidence is incomplete.

If the task requires licensed plug-ins, DAWs, private audio, hardware, GPU
checkpoints, or local services that are unavailable in the current environment,
implement only the repository-contained portion and report the exact local
certification still required. Never replace a missing capability with a silent
no-op or simulated success.
