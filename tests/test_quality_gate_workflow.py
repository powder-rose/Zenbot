import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

BASE = "a" * 40
PREVIOUS = "b" * 40
MERGE_BASE = "c" * 40


@pytest.fixture
def workflow_case(monkeypatch):
    workflow = (
        Path(__file__).resolve().parents[1] / ".github/workflows/quality-gate.yml"
    )
    # Execute the actual Python block from the workflow, not a copied version.
    # This extractor only handles this named step and its literal run block.
    content = workflow.read_text(encoding="utf-8")
    marker = "      - name: Run Quality Gate\n"
    assert content.count(marker) == 1
    step = content.split(marker, 1)[1].split("      - name:", 1)[0]
    assert "        shell: python\n" in step
    script_lines = step.split("        run: |\n", 1)[1].splitlines()
    body = []
    for line in script_lines:
        if line.strip() and not line.startswith("          "):
            break
        body.append(line)
    script = compile(dedent("\n".join(body)), str(workflow), "exec")

    commands = []
    state = SimpleNamespace(base_exists=True)

    def run(command, **kwargs):
        commands.append((command, kwargs))
        if command[:3] == ["git", "cat-file", "-e"]:
            return SimpleNamespace(returncode=0 if state.base_exists else 128)
        if command[:2] == [sys.executable, "scripts/quality_gate.py"]:
            return SimpleNamespace(returncode=0)
        raise AssertionError(f"Unexpected subprocess: {command!r}")

    merge_base = Mock(return_value=f"{MERGE_BASE}\n")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", merge_base)

    def execute(event, base, *, base_exists=True):
        monkeypatch.setenv("GATE_EVENT", event)
        monkeypatch.setenv("GATE_BASE_REF", base)
        state.base_exists = base_exists
        # All subprocess calls above are fakes: no Git mutations, package
        # installations or nested Quality Gate runs happen in these tests.
        exec(script, {"__name__": "__main__"})

    return SimpleNamespace(
        execute=execute,
        commands=commands,
        merge_base=merge_base,
    )


@pytest.mark.parametrize(
    ("event", "base", "base_exists", "expected_base"),
    [
        ("pull_request", BASE, True, BASE),
        ("push", PREVIOUS, True, PREVIOUS),
        ("push", "0" * 40, False, MERGE_BASE),
        ("push", PREVIOUS, False, MERGE_BASE),
    ],
    ids=["pull-request", "push", "first-branch-push", "unavailable-previous-commit"],
)
def test_workflow_runs_gate_against_the_correct_commit(
    workflow_case, event, base, base_exists, expected_base
):
    case = workflow_case

    case.execute(event, base, base_exists=base_exists)

    assert len(case.commands) == 2
    assert case.commands[0][0] == ["git", "cat-file", "-e", base + "^{commit}"]
    assert case.commands[1][0] == [
        sys.executable,
        "scripts/quality_gate.py",
        "--base-ref",
        expected_base,
    ]
    assert case.commands[1][1]["check"] is True
    if expected_base == MERGE_BASE:
        case.merge_base.assert_called_once_with(
            ["git", "merge-base", "HEAD", "origin/master"], text=True
        )
    else:
        case.merge_base.assert_not_called()


@pytest.mark.parametrize("invalid_base", ["", "z" * 40], ids=["missing", "non-hex"])
def test_workflow_rejects_invalid_base_before_starting_commands(
    workflow_case, invalid_base
):
    case = workflow_case

    with pytest.raises(SystemExit, match="valid Git base SHA"):
        case.execute("push", invalid_base)

    assert case.commands == []
    case.merge_base.assert_not_called()
