"""The development agent's hands: a shell and file tools scoped to one project.

Every path is resolved inside the project directory and every command runs
with the project directory as cwd. This is a guard rail against an LLM
wandering off, not a security sandbox — run botpy8_soul on a machine you're fine
letting an LLM run shell commands on.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

IGNORED_DIRS = {
    "node_modules", ".next", ".git", "dist", "build", "out", "coverage",
    "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".turbo",
}
MAX_OUTPUT_CHARS = 6000
MAX_READ_CHARS = 60000
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 1200

# Commands that either never exit (dev servers, watchers) or belong to the
# publisher (git). The agent gets an explanation instead of a hung pipeline.
_BLOCKED = [
    (re.compile(r"(^|[\s;&|])git(\s|$)"), "git is managed by the publisher; don't run git commands"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+(run\s+)?(dev|start|watch)\b"), "dev servers never exit; use `pnpm run build` to check the app compiles"),
    (re.compile(r"\bnext\s+(dev|start)\b"), "dev servers never exit; use `pnpm run build` to check the app compiles"),
    (re.compile(r"--watch\b"), "watch mode never exits; run the command once"),
]

def _find_git_bash() -> str | None:
    """LLMs write POSIX shell (`mkdir -p`, `wc -l`, `&&` chains), and cmd.exe
    silently mangles it. On Windows, Git for Windows ships a bash next to
    git.exe; prefer it. (Not the System32 bash.exe, which is WSL.)"""
    git = shutil.which("git")
    if not git:
        return None
    # git.exe may be in <Git>\cmd, <Git>\bin or <Git>\mingw64\bin.
    for root in Path(git).resolve().parents:
        for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if candidate.exists():
                return str(candidate)
    return None


BASH = _find_git_bash() if os.name == "nt" else None
if os.name != "nt":
    SHELL_NAME = "/bin/sh (POSIX)"
elif BASH:
    SHELL_NAME = "bash (Git Bash on Windows; POSIX syntax, Windows paths)"
else:
    SHELL_NAME = "cmd.exe (Windows; not POSIX — no mkdir -p, wc, rm -rf)"


def resolve_inside(project_dir: Path, rel_path: str) -> Path:
    project_root = project_dir.resolve()
    resolved = (project_root / rel_path).resolve()
    if resolved != project_root and project_root not in resolved.parents:
        raise ValueError(f"path {rel_path!r} escapes project directory {project_dir}")
    return resolved


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return f"{text[:head]}\n... [{len(text) - limit} chars truncated] ...\n{text[-tail:]}"


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _command_env() -> dict:
    env = dict(os.environ)
    # Keep scaffolders and installers non-interactive and their output plain.
    env.update(
        CI="1",
        npm_config_yes="true",
        npm_config_fund="false",
        npm_config_audit="false",
        # CI=1 makes pnpm default to --frozen-lockfile, which breaks scaffolding
        # and `pnpm add`; verification passes the flag explicitly instead.
        npm_config_frozen_lockfile="false",
        NEXT_TELEMETRY_DISABLED="1",
        FORCE_COLOR="0",
        NO_COLOR="1",
    )
    # Secrets the pipeline needs are none of the generated project's business.
    env.pop("OPENROUTER_API_KEY", None)
    return env


def run_command(project_dir: Path, command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    for pattern, reason in _BLOCKED:
        if pattern.search(command):
            return {"exit_code": None, "timed_out": False, "output": f"Refused: {reason}."}
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    proc = subprocess.Popen(
        [BASH, "-c", command] if BASH else command,
        shell=not BASH,
        cwd=project_dir,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=_command_env(),
        start_new_session=os.name != "nt",
    )
    try:
        raw, _ = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raw, _ = proc.communicate()
        timed_out = True
    output = (raw or b"").decode("utf-8", errors="replace")
    return {
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "output": _truncate(output),
    }


def format_command_result(result: dict) -> str:
    if result["timed_out"]:
        status = "TIMED OUT (process killed)"
    elif result["exit_code"] is None:
        status = "NOT RUN"
    else:
        status = f"exit code {result['exit_code']}"
    return f"[{status}]\n{result['output']}".rstrip()


def write_file(project_dir: Path, rel_path: str, content: str) -> str:
    target = resolve_inside(project_dir, rel_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {rel_path}"


def read_file(project_dir: Path, rel_path: str) -> str:
    target = resolve_inside(project_dir, rel_path)
    if not target.is_file():
        return f"error: {rel_path} is not a file"
    return _truncate(target.read_text(encoding="utf-8", errors="replace"), MAX_READ_CHARS)


def list_files(project_dir: Path, rel_path: str = ".") -> str:
    root = resolve_inside(project_dir, rel_path)
    if not root.is_dir():
        return f"error: {rel_path} is not a directory"
    project_root = project_dir.resolve()
    entries = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(filenames):
            entries.append(Path(dirpath, name).relative_to(project_root).as_posix())
        if len(entries) > 400:
            entries.append("... (listing truncated)")
            break
    return "\n".join(entries) or "(empty)"


def _read_package_json(project_dir: Path) -> dict | None:
    pkg_path = project_dir / "package.json"
    if not pkg_path.exists():
        return None
    try:
        return json.loads(pkg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _has_real_test_script(scripts: dict) -> bool:
    test = scripts.get("test", "")
    return bool(test) and "no test specified" not in test


def package_manager(project_dir: Path) -> str:
    """pnpm unless the project was clearly set up with something else."""
    if (project_dir / "package-lock.json").exists() and not (project_dir / "pnpm-lock.yaml").exists():
        return "npm"
    return "pnpm"


def _install_command(pm: str) -> str:
    # Always install, even with node_modules present: a lockfile out of sync
    # with package.json works on the agent's machine and breaks a fresh clone.
    return "pnpm install --frozen-lockfile" if pm == "pnpm" else "npm ci"


def verification_commands(project_dir: Path) -> list[str]:
    """The checks a project must pass, derived from what's on disk.

    Detecting by files rather than by the plan's free-text stack means a
    Next.js app, a Vite app and a plain Node CLI all get verified by their
    own scripts.
    """
    pkg = _read_package_json(project_dir)
    if pkg is not None:
        scripts = pkg.get("scripts", {}) if isinstance(pkg, dict) else {}
        pm = package_manager(project_dir)
        commands = [_install_command(pm)]
        if "build" in scripts:
            commands.append(f"{pm} run build")
        if _has_real_test_script(scripts):
            commands.append(f"{pm} test")
        return commands
    if any(project_dir.glob("*.py")) or (project_dir / "pyproject.toml").exists() \
            or (project_dir / "tests").is_dir():
        return [f'"{sys.executable}" -m pytest -q']
    if (project_dir / "Makefile").exists():
        return ["make test"]
    return []


def verify_project(project_dir: Path) -> tuple[bool, str]:
    pkg = _read_package_json(project_dir)
    if pkg == {}:
        return False, "package.json is not valid JSON"
    if pkg is not None:
        scripts = pkg.get("scripts", {})
        if "build" not in scripts and not _has_real_test_script(scripts):
            return False, "package.json defines neither a `build` nor a `test` script"
    commands = verification_commands(project_dir)
    if not commands:
        return False, "no package.json, Python tests, or Makefile found — nothing to verify"
    report = []
    for command in commands:
        result = run_command(project_dir, command, timeout=MAX_TIMEOUT)
        report.append(f"$ {command}\n{format_command_result(result)}")
        if result["exit_code"] != 0:
            return False, "\n\n".join(report)
    return True, "\n\n".join(report)


def lint_project(project_dir: Path) -> tuple[bool, str]:
    pkg = _read_package_json(project_dir)
    if pkg is not None:
        if "lint" not in pkg.get("scripts", {}):
            return True, "no lint script; skipped"
        command = f"{package_manager(project_dir)} run lint"
    else:
        command = f'"{sys.executable}" -m ruff check .'
    result = run_command(project_dir, command, timeout=MAX_TIMEOUT)
    return result["exit_code"] == 0, f"$ {command}\n{format_command_result(result)}"
