"""Run the project's verification suite and return a structured result.

Prefers a project `verify.sh`. Falls back to pytest when a Python test layout
is detected. The point is to run real checks instead of trusting an AI claim.
Returns {"overall": "pass"|"fail"|"no_checks", "steps": [...]}.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

_LOG_TAIL = 2000


def _run(args: list[str], cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args, cwd=str(cwd), capture_output=True, text=True, timeout=600
        )
        out = (proc.stdout + proc.stderr)[-_LOG_TAIL:]
        return {"cmd": " ".join(args), "exit_code": proc.returncode, "log": out}
    except (OSError, subprocess.SubprocessError) as e:
        return {"cmd": " ".join(args), "exit_code": 127, "log": str(e)}


def _has(name: str) -> bool:
    from shutil import which

    return which(name) is not None


def run_verify_suite(
    project_root: str | Path,
    scope: str = "full",
    verify_script: str = "verify.sh",
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    steps: list[dict[str, Any]] = []

    script = root / verify_script
    if script.exists():
        runner = "bash" if _has("bash") else None
        if runner:
            steps.append(_run([runner, str(script), scope], root))
        else:
            steps.append({"cmd": verify_script, "exit_code": 126, "log": "bash not found"})
    elif (root / "pyproject.toml").exists() or (root / "tests").is_dir():
        if _has("python"):
            steps.append(_run(["python", "-m", "pytest", "-q"], root))
        else:
            steps.append({"cmd": "pytest", "exit_code": 127, "log": "python not found"})

    if not steps:
        return {"overall": "no_checks", "steps": []}

    overall = "pass" if all(s["exit_code"] == 0 for s in steps) else "fail"
    failed = [s["cmd"] for s in steps if s["exit_code"] != 0]
    return {"overall": overall, "steps": steps, "failed_steps": failed}
