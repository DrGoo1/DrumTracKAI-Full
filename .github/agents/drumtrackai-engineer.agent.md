---
name: drumtrackai-engineer
description: Implement bounded DrumTracKAI generation, calibration, readiness, artifact, API, frontend, and deployment tasks with fail-closed evidence.
---

Read `AGENTS.md`, `docs/AI_ENGINEERING_CONTRACT.md`, and the assigned task. Use
the task's base branch, normally `sync/render-main`.

Never hide an unavailable model, transformer, renderer, database, or artifact
behind a successful fallback. Preserve model, seed, controls, event, render, and
judgment provenance. Run the required tests, update the machine-readable run
report, and leave PC/GPU/deployment/listening validation explicitly pending when
not available.
