import json
import subprocess
import pytest
from agents import validator_publisher as vp

PLAN = {
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["test_smoke.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal",
}


def _write_plan(base_dir, plan=PLAN):
    plans_dir = base_dir / "plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / f"{plan['slug']}.json").write_text(json.dumps(plan), encoding="utf-8")


def _write_passing_project(base_dir, slug="wylt"):
    project_dir = base_dir / "projects" / slug
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    return project_dir


def _init_git_repo(base_dir):
    subprocess.run(["git", "init", "-q"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=base_dir, check=True)
    (base_dir / "README.md").write_text("root", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=base_dir, check=True)


def test_update_registry_appends_entry_to_new_file(tmp_path):
    vp.update_registry(tmp_path, PLAN)
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert len(registry) == 1
    assert registry[0]["slug"] == "wylt"
    assert registry[0]["path"] == "projects/wylt"


def test_update_registry_appends_to_existing_file(tmp_path):
    (tmp_path / "projects.json").write_text(
        json.dumps([{"slug": "exl", "name": "exl"}]), encoding="utf-8"
    )
    vp.update_registry(tmp_path, PLAN)
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert [p["slug"] for p in registry] == ["exl", "wylt"]


def test_run_does_not_mark_pushed_when_no_remote_configured(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)
    state = {"project_slug": "wylt"}
    result = vp.run(tmp_path, state)
    assert result.get("pushed") is not True
    assert result["phase"] == "publishing"
    assert result["files_committed"] is True
    assert result["registry_updated"] is True


def test_run_increments_retry_count_on_first_smoke_check_failure(tmp_path):
    _init_git_repo(tmp_path)
    plan = dict(PLAN)
    _write_plan(tmp_path, plan)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    state = {"project_slug": "wylt", "retry_count": 0}
    result = vp.run(tmp_path, state)
    assert result["retry_count"] == 1
    assert result["phase"] == "building"


def test_run_marks_failed_after_second_smoke_check_failure(tmp_path):
    _init_git_repo(tmp_path)
    plan = dict(PLAN)
    _write_plan(tmp_path, plan)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    state = {"project_slug": "wylt", "retry_count": 1}
    result = vp.run(tmp_path, state)
    assert result["retry_count"] == 2
    assert result["phase"] == "failed"


def test_resume_skips_already_completed_publish_flags(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)

    calls = []
    original_push = vp.push

    def spy_push(base_dir, slug):
        calls.append(slug)
        return original_push(base_dir, slug)

    monkeypatch.setattr(vp, "push", spy_push)

    state = {
        "project_slug": "wylt",
        "files_committed": True,
        "registry_updated": True,
        "pushed": False,
    }
    vp.resume(tmp_path, state)
    assert calls == ["wylt"]
