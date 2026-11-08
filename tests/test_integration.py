import json
import subprocess
import pytest
import orchestrator
from agents import github
from tests.fake_llm import ScriptedClient, build_python_project


PLAN_JSON = json.dumps({
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["test_smoke.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal terminal output",
})


def _client():
    return ScriptedClient([PLAN_JSON, build_python_project(), [("finish", {"summary": "done"})]])


@pytest.fixture(autouse=True)
def isolated_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "soul.md").write_text("developer profile", encoding="utf-8")
    (tmp_path / "README.md").write_text("root", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    yield tmp_path


def test_dry_run_produces_project_without_pushing(tmp_path):
    fake = _client()
    result = orchestrator.run_cycle(tmp_path, dry_run=True, client=fake)
    assert result["phase"] == "done"
    assert (tmp_path / "projects" / "wylt" / "test_smoke.py").exists()
    assert (tmp_path / "projects" / "wylt" / "README.md").exists()
    assert not (tmp_path / "projects.json").exists()


def test_live_run_publishes_to_bot_repo_and_records_registry(tmp_path, monkeypatch):
    bot_remote = tmp_path.parent / f"{tmp_path.name}_bot_wylt.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bot_remote)], check=True)
    bot = github.BotAccount("botpy8", "t", "botpy8", "1+botpy8@users.noreply.github.com")
    monkeypatch.setattr(github, "load_bot_account", lambda: bot)
    monkeypatch.setattr(github, "ensure_repo", lambda account, name, desc: f"https://github.com/botpy8/{name}")
    monkeypatch.setattr(github, "remote_url", lambda account, name: str(bot_remote))

    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=_client())
    assert result["phase"] == "publishing"  # no botpy8_soul remote in this sandbox
    assert result["code_pushed"] is True
    assert result["registry_updated"] is True
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert registry[0] == registry[0] | {"slug": "wylt", "repo": "https://github.com/botpy8/wylt"}
    log = subprocess.run(["git", "log", "--format=%an", "main"], cwd=bot_remote,
                         capture_output=True, text=True, check=True).stdout
    assert log.strip() == "botpy8"
