# Repository agent rule

Read `AGENTS.md` and `docs/AI_ENGINEERING_CONTRACT.md` before changing code.

For an assigned issue:

1. Read the complete issue and linked specifications.
2. Confirm the declared base branch and create a focused task branch.
3. Implement only the explicit scope.
4. Run the exact validation requested by the issue.
5. Create or update `.ai/run-reports/<task-id>.json`.
6. Validate the report with `python tools/agentctl.py report validate ...`.
7. Open or update a draft pull request.
8. Read PR review comments directly and address them on the same branch.

Never place credentials, private audio, model checkpoints, commercial content,
absolute user paths, or licensed SDK material in Git. Never report unavailable
GPU/model/DAW capabilities as successful.
