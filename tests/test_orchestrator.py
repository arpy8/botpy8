import json
import pytest
import orchestrator


class _StubPlanner:
    @staticmethod
    def run(base_dir, client, state):
        state.update(phase="building", project_slug="wylt", domain="cli-tools")
        return state

    resume = run


class _StubBuilder:
    @staticmethod
    def run(base_dir, client, state):
        state["phase"] = "validating"
        return state

    resume = run


class _StubValidatorPublisher:
    @staticmethod
    def run(base_dir, state):
        state.update(phase="done", files_committed=True, registry_updated=True, pushed=True)
        return state

    resume = run


class _ExplodingBuilder:
    @staticmethod
    def run(base_dir, client, state):
        raise RuntimeError("boom")

    resume = run


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "logs").mkdir()
    yield tmp_path


def test_run_cycle_fresh_start_goes_planning_to_done(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"


def test_run_cycle_resumes_from_malformed_state_json_as_fresh(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text("{not valid", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"


def test_run_cycle_resumes_mid_pipeline_from_existing_phase(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text(
        json.dumps({"phase": "validating", "project_slug": "wylt"}), encoding="utf-8"
    )
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"
    assert result["project_slug"] == "wylt"


def test_run_cycle_dry_run_never_reaches_validator_publisher(monkeypatch, tmp_path):
    calls = []

    class _TrackedValidatorPublisher:
        @staticmethod
        def run(base_dir, state):
            calls.append("called")
            return state

        resume = run

    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _TrackedValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=True, client=object())
    assert calls == []
    assert result["phase"] == "done"


def test_run_cycle_catches_agent_exception_and_marks_failed(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _ExplodingBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "failed"


def test_run_cycle_persists_state_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _StubBuilder)
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["phase"] == "done"
