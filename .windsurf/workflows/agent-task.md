# Agent task workflow

## Implement an issue

1. Read `AGENTS.md`, `docs/AI_ENGINEERING_CONTRACT.md`, and the assigned issue.
2. Resolve the task ID, base branch, execution target, scope, exclusions,
   acceptance criteria, validation, and local certification.
3. Create a focused task branch without overwriting another agent's work.
4. Implement only the explicit scope and add failure-path tests.
5. Run narrow tests and the requested validation.
6. Create or update `.ai/run-reports/<task-id>.json`.
7. Validate it with `python tools/agentctl.py report validate <report>`.
8. Review the diff for secrets, private assets, binaries, absolute paths, and
   false capability claims.
9. Commit, push, and open a draft PR linked to the issue.

## Address PR review

1. Read the issue/task, PR description, unresolved review threads, CI, and run
   report.
2. Implement blocking and requested in-scope fixes only.
3. Rerun affected validation and update the run report.
4. Reply to review threads with the change and evidence.
5. Record out-of-scope suggestions instead of silently expanding the branch.

## Certify a local build

Use only on an authorized Mac or PC with the required DAW, plug-ins, hardware,
GPU, model checkpoint, private test assets, or deployment access.

1. Start from the exact PR commit.
2. Run the named certification suite.
3. Record pass, fail, not-supported, skipped, or blocked for every test.
4. Store only sanitized logs and approved content hashes.
5. Create `certification/<task-id>.json` and validate it with `agentctl.py`.
6. Never upload private audio, licensed content, credentials, model weights, or
   absolute workstation paths.
