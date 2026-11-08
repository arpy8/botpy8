import json
import subprocess
import pytest
from agents import github
from agents import validator_publisher as vp
from utils import state as state_mod

REAL_ENSURE_REPO = github.ensure_repo  # the autouse fixture replaces it
BOT = github.BotAccount("botpy8", "bot-token-123", "botpy8", "145050801+botpy8@users.noreply.github.com")


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


@pytest.fixture(autouse=True)
def fake_bot_github(tmp_path, monkeypatch):
    """The bot's GitHub is a local bare repo; the API calls are stubbed."""
    remotes = tmp_path.parent / f"{tmp_path.name}_bot_remotes"
    created = []

    def ensure_repo(account, name, description):
        bare = remotes / f"{name}.git"
        if not bare.exists():
            subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
            created.append(name)
        return f"https://github.com/{account.login}/{name}"

    monkeypatch.setattr(github, "load_bot_account", lambda: BOT)
    monkeypatch.setattr(github, "ensure_repo", ensure_repo)
    monkeypatch.setattr(github, "remote_url", lambda account, name: str(remotes / f"{name}.git"))
    return {"remotes": remotes, "created": created}


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
    subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=base_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Owner"], cwd=base_dir, check=True)
    (base_dir / "README.md").write_text("root", encoding="utf-8")
    (base_dir / ".gitignore").write_text("projects/*\nplans/\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=base_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=base_dir, check=True)


def _git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_update_registry_appends_entry_to_new_file(tmp_path):
    vp.update_registry(tmp_path, PLAN, "https://github.com/botpy8/wylt")
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert len(registry) == 1
    assert registry[0]["slug"] == "wylt"
    assert registry[0]["repo"] == "https://github.com/botpy8/wylt"


def test_update_registry_appends_to_existing_file(tmp_path):
    (tmp_path / "projects.json").write_text(
        json.dumps([{"slug": "exl", "name": "exl"}]), encoding="utf-8"
    )
    vp.update_registry(tmp_path, PLAN, "https://github.com/botpy8/wylt")
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert [p["slug"] for p in registry] == ["exl", "wylt"]


def test_run_pushes_project_to_bot_repo_as_bot(tmp_path, fake_bot_github):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)
    result = vp.run(tmp_path, {"project_slug": "wylt"})
    assert result["repo_url"] == "https://github.com/botpy8/wylt"
    assert result["code_pushed"] is True
    assert fake_bot_github["created"] == ["wylt"]
    bare = fake_bot_github["remotes"] / "wylt.git"
    assert _git_out(bare, "log", "-1", "--format=%an <%ae>", "main") == f"botpy8 <{BOT.email}>"
    assert "test_smoke.py" in _git_out(bare, "ls-tree", "--name-only", "main")


def test_soul_repo_keeps_registry_only_and_owner_identity(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)
    vp.run(tmp_path, {"project_slug": "wylt"})
    assert _git_out(tmp_path, "log", "-1", "--format=%an <%ae>") == "Owner <owner@example.com>"
    tracked = _git_out(tmp_path, "ls-files")
    assert "projects.json" in tracked
    assert "projects/wylt" not in tracked


def test_bot_token_never_lands_in_project_git_config(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    project_dir = _write_passing_project(tmp_path)
    vp.run(tmp_path, {"project_slug": "wylt"})
    config = (project_dir / ".git" / "config").read_text(encoding="utf-8")
    assert BOT.token not in config
    assert "extraheader" not in config.lower()
    assert _git_out(project_dir, "config", "--local", "user.email") == BOT.email


def test_bot_git_env_resets_credential_helper_and_scopes_token():
    env = github.bot_git_env(BOT.token)
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["credential.helper"] == ""
    assert pairs["http.https://github.com/.extraheader"].startswith("AUTHORIZATION: basic ")
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert github.TOKEN_ENV not in env


def test_run_does_not_mark_pushed_when_soul_repo_has_no_remote(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)
    result = vp.run(tmp_path, {"project_slug": "wylt"})
    assert result.get("pushed") is not True
    assert result["phase"] == "publishing"
    assert result["code_pushed"] is True
    assert result["registry_updated"] is True


def test_run_increments_retry_count_on_first_smoke_check_failure(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    state = {"project_slug": "wylt", "retry_count": 0}
    result = vp.run(tmp_path, state)
    assert result["retry_count"] == 1
    assert result["phase"] == "building"
    # files are kept for the builder agent to fix, with the failure output
    assert (project_dir / "test_smoke.py").exists()
    assert "assert False" in result["last_failure"]


def test_run_marks_failed_after_second_smoke_check_failure(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    state = {"project_slug": "wylt", "retry_count": 1}
    result = vp.run(tmp_path, state)
    assert result["retry_count"] == 2
    assert result["phase"] == "failed"


def test_resume_skips_already_completed_publish_steps(tmp_path, monkeypatch, fake_bot_github):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)

    calls = []
    original_push = vp.push

    def spy_push(base_dir, slug):
        calls.append(slug)
        return original_push(base_dir, slug)

    monkeypatch.setattr(vp, "push", spy_push)
    monkeypatch.setattr(github, "push_project", lambda *a: pytest.fail("code was already pushed"))

    state = {
        "project_slug": "wylt",
        "repo_url": "https://github.com/botpy8/wylt",
        "code_pushed": True,
        "registry_updated": True,
        "pushed": False,
    }
    vp.resume(tmp_path, state)
    assert calls == ["wylt"]
    assert fake_bot_github["created"] == []


def test_resume_after_push_failure_can_still_push_once_remote_works(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)

    first = vp.run(tmp_path, {"project_slug": "wylt"})
    assert first.get("pushed") is not True
    assert first["phase"] == "publishing"

    bare_remote = tmp_path.parent / f"{tmp_path.name}_soul.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare_remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote)], cwd=tmp_path, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, check=True)

    second = vp.resume(tmp_path, first)
    assert second["pushed"] is True
    assert second["phase"] == "done"


def test_run_flushes_state_to_disk_after_each_publish_substep(tmp_path):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)

    vp.run(tmp_path, {"project_slug": "wylt"})

    on_disk = state_mod.read_state()
    assert on_disk.get("repo_url") == "https://github.com/botpy8/wylt"
    assert on_disk.get("code_pushed") is True
    assert on_disk.get("registry_updated") is True


def test_ensure_repo_reuses_existing_repo_on_resume(monkeypatch):
    responses = {
        ("POST", "/user/repos"): (422, {"message": "name already exists on this account"}),
        ("GET", "/repos/botpy8/wylt"): (200, {"html_url": "https://github.com/botpy8/wylt"}),
    }
    monkeypatch.setattr(github, "_api", lambda method, path, token, body=None: responses[(method, path)])
    assert REAL_ENSURE_REPO(BOT, "wylt", "pitch") == "https://github.com/botpy8/wylt"


def test_ensure_repo_creates_public_repo(monkeypatch):
    sent = {}

    def api(method, path, token, body=None):
        sent.update(body or {})
        return 201, {"html_url": "https://github.com/botpy8/wylt"}

    monkeypatch.setattr(github, "_api", api)
    REAL_ENSURE_REPO(BOT, "wylt", "pitch")
    assert sent["private"] is False and sent["name"] == "wylt"


def test_github_permission_error_keeps_project_publishing_for_retry(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path)
    _write_passing_project(tmp_path)

    def denied(account, name, description):
        raise RuntimeError("could not create repo botpy8/wylt (HTTP 403): Resource not accessible by personal access token")

    monkeypatch.setattr(github, "ensure_repo", denied)
    result = vp.run(tmp_path, {"project_slug": "wylt"})
    assert result["phase"] == "publishing"
    assert "repo_url" not in result
    assert state_mod.read_state()["phase"] == "publishing"
