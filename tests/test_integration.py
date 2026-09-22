import json
import subprocess
import pytest
import orchestrator


class _FakeCompletions:
    def __init__(self, plan_json):
        self._plan_json = plan_json
        self._plan_served = False

    def create(self, **kwargs):
        if not self._plan_served:
            self._plan_served = True
            return _FakeResponse(self._plan_json)
        return _FakeResponse("def test_ok():\n    assert True\n")


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChat:
    def __init__(self, plan_json):
        self.completions = _FakeCompletions(plan_json)


class _FakeClient:
    def __init__(self, plan_json):
        self.chat = _FakeChat(plan_json)


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
    fake = _FakeClient(PLAN_JSON)
    result = orchestrator.run_cycle(tmp_path, dry_run=True, client=fake)
    assert result["phase"] == "done"
    assert (tmp_path / "projects" / "wylt" / "test_smoke.py").exists()
    assert (tmp_path / "projects" / "wylt" / "README.md").exists()
    assert not (tmp_path / "projects.json").exists()


def test_live_run_stages_project_and_registry(tmp_path):
    fake = _FakeClient(PLAN_JSON)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=fake)
    assert result["phase"] == "publishing"
    assert result["files_committed"] is True
    assert result["registry_updated"] is True
    registry = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    assert registry[0]["slug"] == "wylt"
