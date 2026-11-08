import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agents import github, workspace
from agents.builder import load_plan
from utils.state import write_state


def update_registry(base_dir: Path, plan: dict, repo_url: str) -> None:
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
            "repo": repo_url,
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

    ok, report = workspace.verify_project(project_dir)
    if ok:
        ok, report = workspace.lint_project(project_dir)
    if not ok:
        logging.info("[validator] checks failed:\n%s", report)
        state["retry_count"] = state.get("retry_count", 0) + 1
        if state["retry_count"] >= 2:
            state["phase"] = "failed"
        else:
            # Keep the files: the builder agent picks up where it left off and
            # gets the failure output to fix, rather than starting from zero.
            state["phase"] = "building"
            state["last_failure"] = report[-4000:]
        write_state(state)
        return state

    state["phase"] = "publishing"
    write_state(state)

    # Each sub-step is flagged and flushed as soon as it succeeds, so a crash
    # mid-publish resumes without redoing (or skipping) anything.
    account = github.load_bot_account()

    if not state.get("repo_url"):
        state["repo_url"] = github.ensure_repo(account, slug, plan["pitch"])
        write_state(state)

    if not state.get("code_pushed"):
        ok, output = github.push_project(
            project_dir, account, github.remote_url(account, slug), f"feat: {plan['name']} — {plan['pitch']}"[:200]
        )
        if not ok:
            logging.error("[publisher] pushing %s to %s failed:\n%s", slug, state["repo_url"], output)
            write_state(state)
            return state
        state["code_pushed"] = True
        write_state(state)

    if not state.get("registry_updated"):
        update_registry(base_dir, plan, state["repo_url"])
        subprocess.run(["git", "add", "projects.json"], cwd=base_dir)
        state["registry_updated"] = True
        write_state(state)

    if not state.get("pushed"):
        if not push(base_dir, slug):
            write_state(state)
            return state
        state["pushed"] = True

    if state["code_pushed"] and state["registry_updated"] and state["pushed"]:
        state["phase"] = "done"
    write_state(state)
    return state


def resume(base_dir: Path, state: dict) -> dict:
    return run(base_dir, state)
