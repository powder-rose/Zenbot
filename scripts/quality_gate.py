"""Run Zenbot's local Quality Gate with Python 3.12 and requirements-dev.txt.

Windows: ./.venv/Scripts/python.exe ./scripts/quality_gate.py
Linux:   ./.venv/bin/python ./scripts/quality_gate.py

Use --base-ref <commit-or-branch> to include committed changes in Git checks.
The default HEAD includes both staged and unstaged changes. New test/tool
files are checked too. No formatter fixes, installs, commits or deployments
are performed. Pytest writes its normal ignored cache and temporary test data.

PASS requires compatible dependencies, the pinned development tools, critical
Ruff checks on application code, stricter Ruff checks and formatting on tests
and tools, all collected tests passing without skips, and clean Git/text checks.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "ruff.toml"
CRITICAL_RULES = "E9,F63,F7,F82"
EXCLUDED = {".git", ".venv", "venv", "data", "test", "__pycache__"}


def child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environment.pop(name, None)
    environment.update(
        PYTHON_DOTENV_DISABLED="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
    )
    return environment


def run_command(label: str, command: list[str]) -> bool:
    print(f"\n--- {label} ---", flush=True)
    try:
        result = subprocess.run(
            command, cwd=ROOT, env=child_environment(), timeout=180, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"FAIL: {exc}", flush=True)
        return False
    print(f"{label}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
    return result.returncode == 0


def git_output(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", "--no-optional-locks", *arguments],
        cwd=ROOT,
        env=child_environment(),
        encoding="utf-8",
        timeout=30,
    )


def preflight(base_ref: str) -> tuple[list[str], list[str], set[Path], str]:
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Use Python 3.12, matching the production Dockerfile.")
    required_files = (CONFIG, ROOT / "pytest.ini", ROOT / "requirements-dev.txt")
    for path in required_files:
        if not path.is_file():
            raise ValueError(f"Required file is missing: {path.name}")

    pins = {}
    for line in (
        (ROOT / "requirements-dev.txt").read_text(encoding="utf-8-sig").splitlines()
    ):
        match = re.fullmatch(
            r"(pytest|pytest-asyncio|ruff)==([\w.!+\-]+)", line.strip()
        )
        if match:
            pins[match[1]] = match[2]
    for name in ("pytest", "pytest-asyncio", "ruff"):
        if name not in pins:
            raise ValueError(f"Pin {name} in requirements-dev.txt.")
        installed = version(name)
        if installed != pins[name]:
            raise ValueError(f"{name}: installed {installed}, required {pins[name]}.")
        print(f"{name}=={installed}", flush=True)

    if Path(git_output("rev-parse", "--show-toplevel").strip()).resolve() != ROOT:
        raise ValueError("Run the gate from a Zenbot Git checkout.")
    base = git_output(
        "rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"
    ).strip()
    listed = git_output(
        "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"
    )
    files = sorted(
        {
            name
            for name in listed.split("\0")
            if name
            and not EXCLUDED.intersection(Path(name).parts)
            and (ROOT / name).is_file()
        }
    )
    development = [
        name for name in files if Path(name).parts[0] in {"tests", "scripts"}
    ]
    application = [name for name in files if name not in development]
    if not application or not any(
        name.startswith("tests/test_") for name in development
    ):
        raise ValueError("Application files or tests are missing.")
    new_files = git_output(
        "ls-files", "--others", "--exclude-standard", "-z", "--", "*.py"
    )
    text_files = {ROOT / name for name in development}
    text_files.update(ROOT / name for name in new_files.split("\0") if name in files)
    text_files.update(required_files)
    for pattern in ("*.yml", "*.yaml"):
        text_files.update((ROOT / ".github" / "workflows").glob(pattern))
    return application, development, text_files, base


def check_text_files(paths: set[Path]) -> bool:
    print("\n--- Text files, including new files ---", flush=True)
    clean = True
    for path in sorted(paths):
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            print(f"FAIL: {path.relative_to(ROOT)}: {exc}", flush=True)
            clean = False
            continue
        if content and not content.endswith("\n"):
            print(f"FAIL: {path.relative_to(ROOT)}: missing final newline", flush=True)
            clean = False
        for number, line in enumerate(content.splitlines(), 1):
            if line.rstrip(" \t") != line or line.startswith(
                ("<<<<<<<", "=======", ">>>>>>>")
            ):
                print(
                    f"FAIL: {path.relative_to(ROOT)}:{number}: whitespace or merge marker",
                    flush=True,
                )
                clean = False
    print(f"Text files: {'PASS' if clean else 'FAIL'}", flush=True)
    return clean


def run_tests() -> bool:
    with tempfile.NamedTemporaryFile(
        prefix="zenbot-quality-gate-", suffix=".xml", delete=False
    ) as report:
        report_path = Path(report.name)
    try:
        successful = run_command(
            "All tests",
            [
                sys.executable,
                "-m",
                "pytest",
                "-c",
                str(ROOT / "pytest.ini"),
                "-p",
                "pytest_asyncio.plugin",
                "-o",
                "xfail_strict=true",
                "-q",
                "--tb=short",
                f"--junitxml={report_path}",
                "tests",
            ],
        )
        try:
            suites = ElementTree.parse(report_path).iter("testsuite")
            totals = {name: 0 for name in ("tests", "failures", "errors", "skipped")}
            for suite in suites:
                for name in totals:
                    totals[name] += int(suite.get(name, "0"))
        except (OSError, ElementTree.ParseError, ValueError) as exc:
            print(f"FAIL: cannot verify test results: {exc}", flush=True)
            return False
        print(
            "Test totals: "
            + ", ".join(f"{name}={count}" for name, count in totals.items()),
            flush=True,
        )
        return (
            successful
            and totals["tests"] > 0
            and all(totals[name] == 0 for name in ("failures", "errors", "skipped"))
        )
    finally:
        # Delete only the report file created above; never a directory or data.
        report_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local Zenbot Quality Gate.")
    parser.add_argument(
        "--base-ref",
        default="HEAD",
        help="Git base for changed-line checks (default: HEAD)",
    )
    arguments = parser.parse_args()
    print("Zenbot Quality Gate", flush=True)
    try:
        application, development, text_files, base = preflight(arguments.base_ref)
    except (
        OSError,
        ValueError,
        PackageNotFoundError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Preflight: FAIL: {exc}\nQuality Gate: FAIL", flush=True)
        return 1

    ruff = [sys.executable, "-m", "ruff"]
    results = {
        "Dependencies": run_command(
            "Dependencies", [sys.executable, "-m", "pip", "--isolated", "check"]
        ),
        "Application lint": run_command(
            "Application lint",
            [
                *ruff,
                "check",
                "--config",
                str(CONFIG),
                "--no-fix",
                "--select",
                CRITICAL_RULES,
                *application,
            ],
        ),
        "Tests and tools lint": run_command(
            "Tests and tools lint",
            [*ruff, "check", "--config", str(CONFIG), "--no-fix", *development],
        ),
        "Tests and tools format": run_command(
            "Tests and tools format",
            [*ruff, "format", "--config", str(CONFIG), "--check", *development],
        ),
        "All tests": run_tests(),
        "Git changed lines": run_command(
            "Git changed lines",
            ["git", "--no-optional-locks", "diff", "--check", base, "--"],
        ),
        "New and development text": check_text_files(text_files),
    }
    print("\nQuality Gate results:", flush=True)
    for name, passed in results.items():
        print(f"  {'PASS' if passed else 'FAIL'}: {name}", flush=True)
    passed = all(results.values())
    print(f"\nQuality Gate: {'PASS' if passed else 'FAIL'}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
