import json
import logging
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


def test_configure_environment_chdirs_to_base_dir(tmp_path):
    subdir = tmp_path / "repo"
    subdir.mkdir()
    orchestrator.configure_environment(subdir)
    import os

    assert os.getcwd() == str(subdir.resolve())


def test_configure_environment_loads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=from-dotenv\n", encoding="utf-8")
    orchestrator.configure_environment(tmp_path)
    import os

    assert os.environ.get("OPENROUTER_API_KEY") == "from-dotenv"


def test_setup_logging_writes_to_both_file_and_console(tmp_path):
    orchestrator.setup_logging(tmp_path)
    root = logging.getLogger()
    handler_types = {type(h) for h in root.handlers}
    assert logging.StreamHandler in handler_types or any(
        type(h) is logging.StreamHandler for h in root.handlers
    )
    assert any(isinstance(h, logging.FileHandler) for h in root.handlers)


class _QuotaBuilder:
    @staticmethod
    def run(base_dir, client, state):
        from datetime import datetime, timezone
        from utils.llm import QuotaExhausted
        raise QuotaExhausted("free-model", datetime(2030, 1, 1, tzinfo=timezone.utc))

    resume = run


def test_quota_exhaustion_pauses_instead_of_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(orchestrator, "planner", _StubPlanner)
    monkeypatch.setattr(orchestrator, "builder", _QuotaBuilder)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "building"
    assert result["project_slug"] == "wylt"
    assert result["paused_until"].startswith("2030-01-01T00:02")
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["phase"] == "building"


def test_paused_cycle_does_no_work_until_the_reset(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps(
        {"phase": "building", "project_slug": "wylt", "paused_until": "2999-01-01T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(orchestrator, "builder", _ExplodingBuilder)  # would fail if called
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "building"
    assert orchestrator.paused_for(result) > 0


def test_cycle_after_the_pause_resumes_and_clears_it(monkeypatch, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps(
        {"phase": "validating", "project_slug": "wylt", "paused_until": "2000-01-01T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(orchestrator, "validator_publisher", _StubValidatorPublisher)
    result = orchestrator.run_cycle(tmp_path, dry_run=False, client=object())
    assert result["phase"] == "done"
    assert "paused_until" not in result


def test_run_lock_allows_only_one_holder(tmp_path):
    from utils.lock import AlreadyRunning, RunLock

    lock_path = tmp_path / ".botpy8_soul.lock"
    with RunLock(lock_path):
        with pytest.raises(AlreadyRunning):
            with RunLock(lock_path):
                pass
    with RunLock(lock_path):  # released on exit, so it can be taken again
        pass


def test_second_process_exits_while_lock_is_held(tmp_path):
    import subprocess
    import sys
    from utils.lock import RunLock

    script = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from pathlib import Path; from utils.lock import RunLock, AlreadyRunning\n"
        "try:\n    RunLock(Path(sys.argv[2])).__enter__(); print('acquired')\n"
        "except AlreadyRunning: print('busy')"
    )
    repo = str(orchestrator.Path(orchestrator.__file__).resolve().parent)
    lock_path = tmp_path / ".botpy8_soul.lock"
    with RunLock(lock_path):
        busy = subprocess.run([sys.executable, "-c", script, repo, str(lock_path)], capture_output=True, text=True)
    free = subprocess.run([sys.executable, "-c", script, repo, str(lock_path)], capture_output=True, text=True)
    assert busy.stdout.strip() == "busy"
    assert free.stdout.strip() == "acquired"
