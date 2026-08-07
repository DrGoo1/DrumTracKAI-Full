# AI Engineering Contract

This repository uses GitHub—not chat transcripts—as the durable coordination
plane between ChatGPT, Codex, VS Code Copilot, Windsurf, local machines, and
human reviewers.

## Source-of-truth chain

Every substantial change follows this chain:

```text
product decision
→ GitHub issue or `.ai/tasks/<task-id>.json`
→ focused task branch
→ implementation commits
→ `.ai/run-reports/<task-id>.json`
→ pull request
→ automated validation
→ required local certification
→ review
→ merge
```

A conversation may explain intent, but executable scope and acceptance criteria
must be present in the issue or task document before implementation starts.

## Required task content

A task must identify:

- repository and base branch;
- execution target (`cloud`, `mac-local`, `pc-local`, `hybrid`, or `manual`);
- preferred agent;
- objective;
- explicit in-scope and out-of-scope work;
- acceptance criteria;
- validation commands;
- required evidence;
- local capabilities and data classification when relevant.

Validate task files with:

```bash
python tools/agentctl.py task validate .ai/tasks/<task-id>.json
python tools/agentctl.py task branch-plan .ai/tasks/<task-id>.json
```

## Agent responsibilities

### ChatGPT

- product and systems architecture;
- cross-repository contracts;
- task decomposition and acceptance criteria;
- pull-request and validation review;
- identifying architectural drift and unsupported claims.

### Codex or cloud coding agents

Use for repository-contained work that does not require licensed software,
private audio, local model checkpoints, hardware, or DAW interaction.

### VS Code Copilot on the studio Mac

Use for macOS-native code, AU/VST3/JUCE work, LUNA and other DAW integration,
VSTi rendering, Apple-silicon validation, and local audio workflows.

### Windsurf on the PC

Use for DrumTracKAI backend and calibration work, Windows builds, GPU/model
operations, checkpoint validation, and services or datasets available only on
the PC.

### Human product owner

Approves musical and product intent, listens to candidates, resolves ambiguous
scope, authorizes sensitive actions, and decides when a draft PR may merge.

## Branch and ownership rules

- One bounded task, one branch, one active implementation owner.
- Never make overlapping changes on another agent's unmerged branch.
- Start from the task's declared base branch.
- Never force-push a shared branch.
- Do not commit directly to `main`, `master`, or a deployment branch.
- A task branch should normally be `agent/<agent>-<task>` or
  `feature/<task>`.

## Run reports

Substantial work must produce a run report:

```bash
python tools/agentctl.py report create \
  --task .ai/tasks/<task-id>.json \
  --output .ai/run-reports/<task-id>.json \
  --branch "$(git branch --show-current)" \
  --commit "$(git rev-parse HEAD)" \
  --status completed

python tools/agentctl.py report validate \
  .ai/run-reports/<task-id>.json
```

The report records exact files, commands, results, deviations, artifacts, and
known limitations. “Tests pass” without commands and results is not evidence.

## Local certification

DAW, plug-in, licensed-content, hardware, GPU, model-checkpoint, and
audio-listening validation must run on an authorized local machine. Record the
result in a certification report and commit only sanitized logs and hashes.

```bash
python tools/agentctl.py certification validate \
  certification/<task-id>.json
```

Never commit:

- private or commercial audio;
- DAW sessions containing private material;
- model weights or checkpoints unless explicitly approved;
- plugin licenses, SDK credentials, signing identities, or certificates;
- absolute user paths;
- access tokens, passwords, or secrets;
- proprietary presets, samples, or schematics.

## Pull-request requirements

A draft PR must include:

- task or issue identity;
- behavior added or changed;
- explicit scope and exclusions;
- run-report path;
- exact validation results;
- local certification status;
- migration and compatibility impact;
- privacy, real-time, deployment, and safety impact;
- unresolved limitations.

Keep the PR in draft while required evidence is missing.

## Capability truth

Agents must distinguish:

```text
implemented
unit-tested
fixture-validated
real-data-validated
locally-certified
user-validated
autonomous-safe
unavailable
```

Code existing is not equivalent to a capability being validated. Unavailable
or degraded dependencies must fail closed and be reported explicitly.

## Review protocol

Reviewers inspect the issue/task, diff, run report, CI, and any local
certification. Review feedback belongs on the PR. The implementation agent
reads and addresses those comments directly; the user should not need to relay
review prose between tools.

## Control-plane schemas

Versioned schemas live under `.ai/`. Backward-incompatible changes require a
new schema version and migration guidance. Validate all examples with:

```bash
python tools/agentctl.py self-test
python tools/agentctl.py examples validate
```

# Repository execution matrix

Repository: `DrGoo1/DrumTracKAI-Full`  
Declared integration base: `sync/render-main`  
Primary local lane: **PC / Windsurf**

| Work type | Preferred lane | Required evidence |
|---|---|---|
| Architecture and StudioMind contracts | ChatGPT | Task specification and cross-repo review |
| Python services, schemas, APIs, React UI | Codex, cloud Copilot, or Windsurf | CI and run report |
| Calibration website and artifact flows | PC / Windsurf | Backend/frontend tests and deployed smoke evidence |
| CUDA, ONNX, checkpoints, readiness | PC / Windsurf | Model manifest, hashes, readiness and inference tests |
| Windows connector/plugin builds | PC / Windsurf | Windows local certification |
| Listening calibration | Human | Judgment session and artifact IDs |

Cloud agents must not claim checkpoint readiness, GPU inference, Render
deployment, licensed audio rendering, or listening validation.

## Routing labels

Use combinations of:

- `agent:chatgpt`, `agent:codex`, `agent:copilot`, `agent:windsurf`, `agent:human`
- `machine:cloud`, `machine:mac-audio`, `machine:pc-gpu`
- `status:designed`, `status:ready`, `status:implementing`, `status:review`,
  `status:blocked`, `status:validated`
- `risk:database`, `risk:realtime`, `risk:privacy`, `risk:daw`, `risk:model`,
  `risk:artifact`, `risk:deployment`

## Cross-repository boundaries

| Repository | Authority |
|---|---|
| `DrGoo1/StudioMind_AI` | Producer orchestration, UMPM, project modes, reference intelligence, DAW adapters, VSTi realization, approval and rollback |
| `DrGoo1/DrumTracKAI-Full` | Drum performance generation, calibration, drummer profiles, model readiness, candidate artifacts |
| Future `TrackAI_SDK` | Shared versioned contracts only; no product data or model weights |

The repositories communicate through versioned services and artifact contracts.
StudioMind must not import DrumTracKAI internal modules. DrumTracKAI must report
the exact engine/model version and capabilities actually applied. Breaking
contract changes require linked tasks and compatibility fixtures in both
repositories. Private audio, checkpoints, commercial references, and DAW
sessions remain outside Git.
