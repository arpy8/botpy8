import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agents.builder import load_plan, run_smoke_check
from utils.state import write_state


def run_lint(project_dir: Path, stack: list[str]) -> bool:
    lowered = [s.lower() for s in stack]
    if any("python" in s for s in lowered):
        result = subprocess.run(
            ["ruff", "check", "."], cwd=project_dir, capture_output=True, text=True
        )
        return result.returncode == 0
    result = subprocess.run(
        ["npx", "eslint", "."], cwd=project_dir, capture_output=True, text=True
    )
    return result.returncode == 0


def commit_and_stage(base_dir: Path, slug: str) -> bool:
    result = subprocess.run(
        ["git", "add", f"projects/{slug}"],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def update_registry(base_dir: Path, plan: dict) -> None:
    registry_path = base_dir / "projects.json"
    registry = []
    if registry_path.exists():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            registry = []
    registry.append(
        {
            "name": plan["name"],
            "slug": plan["slug"],
            "pitch": plan["pitch"],
            "story": plan["story"],
            "domain": plan["domain"],
            "stack": plan["stack"],
            "path": f"projects/{plan['slug']}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    registry_path.write_text(json.dumps(registry, indent=2), encoding="utf-8")


def _has_staged_changes(base_dir: Path) -> bool:
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def push(base_dir: Path, slug: str) -> bool:
    if _has_staged_changes(base_dir):
        commit_result = subprocess.run(
            ["git", "commit", "-m", f"feat: add {slug}"],
            cwd=base_dir,
            capture_output=True,
            text=True,
        )
        if commit_result.returncode != 0:
            return False
    push_result = subprocess.run(
        ["git", "push", "origin", "main"],
        cwd=base_dir,
        capture_output=True,
        text=True,
    )
    return push_result.returncode == 0


def run(base_dir: Path, state: dict) -> dict:
    slug = state["project_slug"]
    plan = load_plan(slug, base_dir)
    project_dir = base_dir / "projects" / slug

    if not run_lint(project_dir, plan["stack"]) or not run_smoke_check(project_dir, plan["stack"]):
        state["retry_count"] = state.get("retry_count", 0) + 1
        if state["retry_count"] >= 2:
            state["phase"] = "failed"
        else:
            state["phase"] = "building"
            shutil.rmtree(project_dir, ignore_errors=True)
        write_state(state)
        return state

    state["phase"] = "publishing"
    write_state(state)

    if not state.get("files_committed"):
        if not commit_and_stage(base_dir, slug):
            write_state(state)
            return state
        state["files_committed"] = True
        write_state(state)

    if not state.get("registry_updated"):
        update_registry(base_dir, plan)
        subprocess.run(["git", "add", "projects.json"], cwd=base_dir)
        state["registry_updated"] = True
        write_state(state)

    if not state.get("pushed"):
        if not push(base_dir, slug):
            write_state(state)
            return state
        state["pushed"] = True

    if state["files_committed"] and state["registry_updated"] and state["pushed"]:
        state["phase"] = "done"
    write_state(state)
    return state


def resume(base_dir: Path, state: dict) -> dict:
    return run(base_dir, state)
