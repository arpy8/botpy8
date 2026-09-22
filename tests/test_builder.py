import json
import pytest
from agents import builder


class _FakeCompletions:
    def __init__(self, default_content="generated content"):
        self.default_content = default_content
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.default_content)


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
    def __init__(self, default_content):
        self.completions = _FakeCompletions(default_content)


class _FakeClient:
    def __init__(self, default_content="generated content"):
        self.chat = _FakeChat(default_content)


PLAN = {
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["main.py", "utils.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal terminal output",
}


def test_load_plan_reads_json_by_slug(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    (plans_dir / "wylt.json").write_text(json.dumps(PLAN), encoding="utf-8")
    assert builder.load_plan("wylt", tmp_path) == PLAN


def test_scaffold_files_writes_each_structure_entry(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    fake = _FakeClient(default_content="print('hi')")
    builder.scaffold_files(PLAN, project_dir, fake)
    assert (project_dir / "main.py").read_text(encoding="utf-8") == "print('hi')"
    assert (project_dir / "utils.py").exists()


def test_scaffold_files_rejects_path_traversal(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    fake = _FakeClient(default_content="ignored")
    plan = dict(PLAN, structure=["../../../escaped.py"])
    with pytest.raises(ValueError, match="escapes project directory"):
        builder.scaffold_files(plan, project_dir, fake)
    assert not (tmp_path / "escaped.py").exists()


def test_scaffold_files_rejects_absolute_path(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    fake = _FakeClient(default_content="ignored")
    outside = tmp_path / "outside.py"
    plan = dict(PLAN, structure=[str(outside)])
    with pytest.raises(ValueError, match="escapes project directory"):
        builder.scaffold_files(plan, project_dir, fake)
    assert not outside.exists()


def test_scaffold_files_skips_existing_files(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "main.py").write_text("already here", encoding="utf-8")
    fake = _FakeClient(default_content="overwritten")
    builder.scaffold_files(PLAN, project_dir, fake)
    assert (project_dir / "main.py").read_text(encoding="utf-8") == "already here"
    assert (project_dir / "utils.py").read_text(encoding="utf-8") == "overwritten"


def test_generate_readme_writes_file(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    fake = _FakeClient(default_content="# wylt\n\nfull readme")
    builder.generate_readme(PLAN, project_dir, fake)
    assert "full readme" in (project_dir / "README.md").read_text(encoding="utf-8")


def test_generate_readme_skips_when_already_present(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "README.md").write_text("existing readme", encoding="utf-8")
    fake = _FakeClient(default_content="new readme")
    builder.generate_readme(PLAN, project_dir, fake)
    assert (project_dir / "README.md").read_text(encoding="utf-8") == "existing readme"


def test_run_smoke_check_picks_pytest_for_python_stack(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    assert builder.run_smoke_check(project_dir, ["python"]) is True


def test_run_smoke_check_returns_false_on_failing_test(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "test_smoke.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )
    assert builder.run_smoke_check(project_dir, ["python"]) is False


def test_run_sets_phase_validating_on_success(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan = dict(PLAN, structure=["test_smoke.py"])
    (plans_dir / "wylt.json").write_text(json.dumps(plan), encoding="utf-8")
    fake = _FakeClient(default_content="def test_ok():\n    assert True\n")
    state = {"project_slug": "wylt"}
    result = builder.run(tmp_path, fake, state)
    assert result["phase"] == "validating"


def test_run_sets_phase_failed_on_smoke_check_failure(tmp_path):
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan = dict(PLAN, structure=["test_smoke.py"])
    (plans_dir / "wylt.json").write_text(json.dumps(plan), encoding="utf-8")
    fake = _FakeClient(default_content="def test_fails():\n    assert False\n")
    state = {"project_slug": "wylt"}
    result = builder.run(tmp_path, fake, state)
    assert result["phase"] == "failed"
