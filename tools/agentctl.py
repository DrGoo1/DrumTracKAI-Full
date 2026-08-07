#!/usr/bin/env python3
"""Validate and summarize repository-native AI engineering task artifacts.

The tool intentionally uses only the Python standard library by default. When
``jsonschema`` is installed it performs full Draft 2020-12 validation; otherwise
it falls back to conservative required-field and type checks so local agents can
still fail closed on malformed control-plane artifacts.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - exercised on minimal local installs
    Draft202012Validator = None  # type: ignore[assignment]
    FormatChecker = None  # type: ignore[assignment]

SCHEMA_VERSION = "1.0.0"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_RELATIVE_PATH_RE = re.compile(r"^(?!/)(?![A-Za-z]:[\\/])(?!.*(?:^|/)\.\.(?:/|$)).+$")
SECRET_VALUE_PATTERNS = (
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*\S+"),
)
FORBIDDEN_KEY_FRAGMENTS = (
    "password",
    "private_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "github_token",
    "pat",
)

SCHEMA_FILES = {
    "task": "task.schema.json",
    "run-report": "run-report.schema.json",
    "certification": "certification-report.schema.json",
}


class ValidationFailure(Exception):
    """Raised when a control-plane artifact is invalid."""


def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationFailure(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationFailure(
            f"Invalid JSON in {path}: line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except OSError as exc:
        raise ValidationFailure(f"Unable to read {path}: {exc}") from exc


def _load_schema(kind: str) -> Mapping[str, Any]:
    schema_path = repository_root() / ".ai" / SCHEMA_FILES[kind]
    schema = _load_json(schema_path)
    if not isinstance(schema, dict):
        raise ValidationFailure(f"Schema is not a JSON object: {schema_path}")
    return schema


def _json_pointer(parts: Sequence[Any]) -> str:
    if not parts:
        return "$"
    rendered = "$"
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += "." + str(part)
    return rendered


def _scan_for_secrets(value: Any, path: tuple[Any, ...] = ()) -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS):
                errors.append(f"{_json_pointer((*path, key))}: forbidden secret-bearing key")
            errors.extend(_scan_for_secrets(nested, (*path, key)))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            errors.extend(_scan_for_secrets(nested, (*path, index)))
    elif isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                errors.append(f"{_json_pointer(path)}: value appears to contain a credential")
                break
    return errors


def _require_object(document: Any, source: Path) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValidationFailure(f"{source}: expected a JSON object")
    return document


def _fallback_validate(kind: str, document: dict[str, Any]) -> list[str]:
    required = {
        "task": {
            "schema_version": str,
            "task_id": str,
            "repository": str,
            "base_branch": str,
            "execution_target": str,
            "preferred_agent": str,
            "objective": str,
            "in_scope": list,
            "out_of_scope": list,
            "acceptance_criteria": list,
            "validation": list,
            "required_evidence": list,
            "status": str,
        },
        "run-report": {
            "schema_version": str,
            "task_id": str,
            "repository": str,
            "status": str,
            "branch": str,
            "commit": (str, type(None)),
            "agent": str,
            "changed_files": list,
            "tests": list,
            "deviations": list,
            "known_limitations": list,
            "artifacts": list,
            "generated_at": str,
        },
        "certification": {
            "schema_version": str,
            "task_id": str,
            "repository": str,
            "machine_id": str,
            "execution_target": str,
            "os": str,
            "suite": str,
            "status": str,
            "tests": dict,
            "artifact_hashes": list,
            "generated_at": str,
        },
    }[kind]

    errors: list[str] = []
    for field, expected_type in required.items():
        if field not in document:
            errors.append(f"$.{field}: required property is missing")
            continue
        if not isinstance(document[field], expected_type):
            errors.append(f"$.{field}: expected {expected_type}, got {type(document[field]).__name__}")
    return errors


def _schema_validate(kind: str, document: dict[str, Any]) -> list[str]:
    if Draft202012Validator is None:
        return _fallback_validate(kind, document)

    schema = _load_schema(kind)
    validator = Draft202012Validator(
        schema,
        format_checker=FormatChecker() if FormatChecker is not None else None,
    )
    errors = sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    return [
        f"{_json_pointer(tuple(error.absolute_path))}: {error.message}"
        for error in errors
    ]


def _validate_relative_paths(values: Iterable[Any], field_name: str) -> list[str]:
    errors: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str):
            continue
        if "://" in value or value.startswith("artifact:") or value.startswith("sha256:"):
            continue
        if not SAFE_RELATIVE_PATH_RE.match(value):
            errors.append(
                f"$.{field_name}[{index}]: expected a repository-relative path, artifact ID, or URI"
            )
    return errors


def _validate_common(document: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if document.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"$.schema_version: expected {SCHEMA_VERSION!r}, got {document.get('schema_version')!r}"
        )
    repository = document.get("repository")
    if isinstance(repository, str) and not REPOSITORY_RE.fullmatch(repository):
        errors.append("$.repository: expected owner/name")
    errors.extend(_scan_for_secrets(document))
    return errors


def _validate_task(document: dict[str, Any]) -> list[str]:
    errors = _validate_common(document)
    for field in ("in_scope", "acceptance_criteria", "required_evidence"):
        values = document.get(field)
        if isinstance(values, list) and not any(isinstance(item, str) and item.strip() for item in values):
            errors.append(f"$.{field}: must contain at least one non-blank item")
    branch_name = document.get("branch_name")
    if isinstance(branch_name, str) and branch_name in {"main", "master"}:
        errors.append("$.branch_name: task branches may not be main or master")
    return errors


def _validate_run_report(document: dict[str, Any]) -> list[str]:
    errors = _validate_common(document)
    branch = document.get("branch")
    if isinstance(branch, str) and branch in {"main", "master"}:
        errors.append("$.branch: substantial agent work may not report main or master as its task branch")
    commit = document.get("commit")
    status = document.get("status")
    if status == "completed" and not (isinstance(commit, str) and SHA_RE.fullmatch(commit)):
        errors.append("$.commit: completed reports require a full lowercase 40-character commit SHA")
    if isinstance(commit, str) and not SHA_RE.fullmatch(commit):
        errors.append("$.commit: expected a full lowercase 40-character commit SHA or null")
    changed_files = document.get("changed_files")
    if isinstance(changed_files, list):
        errors.extend(_validate_relative_paths(changed_files, "changed_files"))
    artifacts = document.get("artifacts")
    if isinstance(artifacts, list):
        errors.extend(_validate_relative_paths(artifacts, "artifacts"))
    tests = document.get("tests")
    if isinstance(tests, list):
        for index, test in enumerate(tests):
            if not isinstance(test, dict):
                continue
            if test.get("result") == "pass" and test.get("failed", 0) not in {0, None}:
                errors.append(f"$.tests[{index}]: passing test record cannot report failures")
    return errors


def _validate_certification(document: dict[str, Any]) -> list[str]:
    errors = _validate_common(document)
    hashes = document.get("artifact_hashes")
    if isinstance(hashes, list):
        for index, value in enumerate(hashes):
            if not isinstance(value, str) or not re.fullmatch(r"^[0-9a-f]{64}$", value):
                errors.append(f"$.artifact_hashes[{index}]: expected lowercase SHA-256")
    log_path = document.get("sanitized_log")
    if isinstance(log_path, str):
        errors.extend(_validate_relative_paths([log_path], "sanitized_log"))
    return errors


KIND_VALIDATORS = {
    "task": _validate_task,
    "run-report": _validate_run_report,
    "certification": _validate_certification,
}


def validate_document(kind: str, path: Path) -> dict[str, Any]:
    document = _require_object(_load_json(path), path)
    errors = _schema_validate(kind, document)
    errors.extend(KIND_VALIDATORS[kind](document))
    if errors:
        rendered = "\n".join(f"- {error}" for error in sorted(set(errors)))
        raise ValidationFailure(f"{path} failed {kind} validation:\n{rendered}")
    return document


def _summary_task(document: Mapping[str, Any]) -> str:
    lines = [
        f"Task: {document['task_id']}",
        f"Repository: {document['repository']}",
        f"Base branch: {document['base_branch']}",
        f"Execution: {document['execution_target']} / {document['preferred_agent']}",
        f"Status: {document['status']}",
        "",
        str(document["objective"]).strip(),
        "",
        "Acceptance criteria:",
    ]
    lines.extend(f"- {item}" for item in document.get("acceptance_criteria", []))
    return "\n".join(lines)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def create_report(args: argparse.Namespace) -> None:
    task_path = Path(args.task)
    task = validate_document("task", task_path)
    report = {
        "schema_version": SCHEMA_VERSION,
        "task_id": task["task_id"],
        "repository": task["repository"],
        "status": args.status,
        "branch": args.branch,
        "commit": args.commit,
        "agent": args.agent or task["preferred_agent"],
        "changed_files": [],
        "tests": [],
        "deviations": [],
        "known_limitations": [],
        "artifacts": [],
        "generated_at": _iso_now(),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(output)


def create_pr_body(args: argparse.Namespace) -> None:
    report = validate_document("run-report", Path(args.report))
    test_lines = []
    for test in report.get("tests", []):
        if isinstance(test, dict):
            test_lines.append(
                f"| `{test.get('command', '')}` | {test.get('result', 'unknown')} | "
                f"{test.get('summary', '')} |"
            )
    if not test_lines:
        test_lines.append("| Not recorded | unknown | Add validation evidence before review. |")

    scope_lines = [f"- `{path}`" for path in report.get("changed_files", [])]
    if not scope_lines:
        scope_lines = ["- Not recorded."]
    deviation_lines = [f"- {item}" for item in report.get("deviations", [])]
    if not deviation_lines:
        deviation_lines = ["- None recorded."]
    limitation_lines = [f"- {item}" for item in report.get("known_limitations", [])]
    if not limitation_lines:
        limitation_lines = ["- None recorded."]

    body = "\n".join(
        [
            "## Task",
            f"`{report['task_id']}`",
            "",
            "## Scope completed",
            *scope_lines,
            "",
            "## Validation",
            "| Command | Result | Summary |",
            "|---|---:|---|",
            *test_lines,
            "",
            "## Deviations",
            *deviation_lines,
            "",
            "## Known limitations",
            *limitation_lines,
            "",
            "## Agent run report",
            f"`{Path(args.report).as_posix()}`",
            "",
            "> Keep the pull request in draft until automated checks and any required local "
            "DAW/GPU/audio certification are complete.",
            "",
        ]
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    print(output)


def branch_plan(args: argparse.Namespace) -> None:
    task = validate_document("task", Path(args.task))
    branch = task.get("branch_name")
    if not branch:
        slug = re.sub(r"[^a-z0-9]+", "-", task["task_id"].lower()).strip("-")
        agent = re.sub(r"[^a-z0-9]+", "-", task["preferred_agent"].lower()).strip("-")
        branch = f"agent/{agent}-{slug}"
    print(f"git fetch origin {task['base_branch']}")
    print(f"git switch -c {branch} origin/{task['base_branch']}")


def _example_paths() -> list[tuple[str, Path]]:
    root = repository_root() / ".ai" / "examples"
    return [
        ("task", root / "task.example.json"),
        ("run-report", root / "run-report.example.json"),
        ("certification", root / "certification-report.example.json"),
    ]


def validate_examples() -> None:
    for kind, path in _example_paths():
        validate_document(kind, path)
        print(f"PASS {kind}: {path.relative_to(repository_root())}")


def self_test() -> None:
    validate_examples()

    valid_task = _load_json(repository_root() / ".ai" / "examples" / "task.example.json")
    assert isinstance(valid_task, dict)

    invalid_task = dict(valid_task)
    invalid_task["branch_name"] = "main"
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "invalid-task.json"
        path.write_text(json.dumps(invalid_task), encoding="utf-8")
        try:
            validate_document("task", path)
        except ValidationFailure:
            pass
        else:  # pragma: no cover
            raise AssertionError("invalid task unexpectedly passed")

    invalid_report = _load_json(
        repository_root() / ".ai" / "examples" / "run-report.example.json"
    )
    assert isinstance(invalid_report, dict)
    invalid_report["status"] = "completed"
    invalid_report["commit"] = None
    with tempfile.TemporaryDirectory() as temp_dir:
        path = Path(temp_dir) / "invalid-report.json"
        path.write_text(json.dumps(invalid_report), encoding="utf-8")
        try:
            validate_document("run-report", path)
        except ValidationFailure:
            pass
        else:  # pragma: no cover
            raise AssertionError("invalid completed report unexpectedly passed")

    print("PASS agentctl self-test")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    task = subparsers.add_parser("task", help="Validate, show, or plan a task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_validate = task_sub.add_parser("validate")
    task_validate.add_argument("path")
    task_show = task_sub.add_parser("show")
    task_show.add_argument("path")
    task_plan = task_sub.add_parser("branch-plan")
    task_plan.add_argument("path")

    report = subparsers.add_parser("report", help="Create or validate an agent run report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_validate = report_sub.add_parser("validate")
    report_validate.add_argument("path")
    report_create = report_sub.add_parser("create")
    report_create.add_argument("--task", required=True)
    report_create.add_argument("--output", required=True)
    report_create.add_argument("--branch", required=True)
    report_create.add_argument("--commit")
    report_create.add_argument(
        "--status",
        choices=("completed", "partial", "blocked", "failed"),
        default="partial",
    )
    report_create.add_argument("--agent")

    certification = subparsers.add_parser(
        "certification", help="Validate a local certification report"
    )
    certification_sub = certification.add_subparsers(
        dest="certification_command", required=True
    )
    certification_validate = certification_sub.add_parser("validate")
    certification_validate.add_argument("path")

    examples = subparsers.add_parser("examples", help="Validate repository examples")
    examples_sub = examples.add_subparsers(dest="examples_command", required=True)
    examples_sub.add_parser("validate")

    pr_body = subparsers.add_parser("pr-body", help="Create a PR body from a run report")
    pr_body.add_argument("--report", required=True)
    pr_body.add_argument("--output", required=True)

    subparsers.add_parser("self-test", help="Run deterministic internal tests")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "task":
            path = Path(args.path)
            if args.task_command == "validate":
                validate_document("task", path)
                print(f"PASS task: {path}")
            elif args.task_command == "show":
                print(_summary_task(validate_document("task", path)))
            else:
                branch_plan(argparse.Namespace(task=args.path))
        elif args.command == "report":
            if args.report_command == "validate":
                validate_document("run-report", Path(args.path))
                print(f"PASS run-report: {args.path}")
            else:
                create_report(args)
        elif args.command == "certification":
            validate_document("certification", Path(args.path))
            print(f"PASS certification: {args.path}")
        elif args.command == "examples":
            validate_examples()
        elif args.command == "pr-body":
            create_pr_body(args)
        elif args.command == "self-test":
            self_test()
        else:  # pragma: no cover
            raise AssertionError(f"Unhandled command: {args.command}")
    except ValidationFailure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
