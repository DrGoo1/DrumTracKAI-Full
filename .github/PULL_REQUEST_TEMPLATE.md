## Task

- Task ID / issue:
- Base branch:
- Execution lane:
- Agent:

## Behavior changed

Describe the externally observable behavior.

## Scope

### Included

-

### Explicitly excluded

-

## Validation

| Command or certification | Result | Evidence |
|---|---:|---|
| `python tools/agentctl.py self-test` |  |  |

- Run report: `.ai/run-reports/<task-id>.json`
- Local certification: not required / pending / `certification/<task-id>.json`

## Compatibility and operational impact

- Database or persisted schema:
- API or contract:
- DAW / plug-in / model:
- Deployment:
- Privacy and data:
- Real-time audio:
- Rollback:

## Known limitations

-

## Review checklist

- [ ] Scope matches the task or linked issue.
- [ ] Run report validates.
- [ ] Exact test commands and results are recorded.
- [ ] Required local certification is attached or explicitly pending.
- [ ] No secrets, private audio, licensed content, absolute local paths, or build debris are committed.
- [ ] Unavailable capabilities fail closed and are not reported as successful.
- [ ] Documentation and generated schemas match the implementation.
- [ ] PR remains draft until all required evidence is complete.
