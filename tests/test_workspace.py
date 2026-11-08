import json
import pytest
from agents import workspace


def test_resolve_inside_rejects_traversal_and_absolute(tmp_path):
    with pytest.raises(ValueError, match="escapes project directory"):
        workspace.resolve_inside(tmp_path, "../x")
    with pytest.raises(ValueError, match="escapes project directory"):
        workspace.resolve_inside(tmp_path, str(tmp_path.parent / "x"))


def test_run_command_runs_in_project_dir(tmp_path):
    (tmp_path / "marker.txt").write_text("hello", encoding="utf-8")
    result = workspace.run_command(tmp_path, 'python -c "print(open(\'marker.txt\').read())"')
    assert result["exit_code"] == 0
    assert "hello" in result["output"]


def test_run_command_reports_nonzero_exit(tmp_path):
    result = workspace.run_command(tmp_path, 'python -c "import sys; sys.exit(3)"')
    assert result["exit_code"] == 3


def test_run_command_times_out_and_kills(tmp_path):
    result = workspace.run_command(tmp_path, 'python -c "import time; time.sleep(30)"', timeout=1)
    assert result["timed_out"] is True
    assert "TIMED OUT" in workspace.format_command_result(result)


def test_run_command_hides_pipeline_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")
    result = workspace.run_command(
        tmp_path, 'python -c "import os; print(os.environ.get(\'OPENROUTER_API_KEY\'))"'
    )
    assert "secret-value" not in result["output"]


@pytest.mark.parametrize(
    "command", ["git push origin main", "pnpm dev", "pnpm run dev", "npm start", "npx next dev", "vitest --watch"]
)
def test_run_command_refuses_git_and_never_ending_commands(tmp_path, command):
    result = workspace.run_command(tmp_path, command)
    assert result["exit_code"] is None
    assert result["output"].startswith("Refused")


def test_scaffold_command_with_disable_git_flag_is_allowed(tmp_path):
    result = workspace.run_command(tmp_path, "echo create-next-app --disable-git")
    assert result["exit_code"] == 0


def test_list_files_skips_node_modules(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "index.js").write_text("", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("", encoding="utf-8")
    assert workspace.list_files(tmp_path) == "src/app.ts"


def test_output_truncated_keeps_tail():
    text = "start" + "x" * 20000 + "THE_ERROR"
    out = workspace._truncate(text)
    assert len(out) < 7000
    assert out.endswith("THE_ERROR")


def _package(tmp_path, scripts):
    (tmp_path / "package.json").write_text(json.dumps({"name": "p", "scripts": scripts}), encoding="utf-8")
    (tmp_path / "node_modules").mkdir(exist_ok=True)


def test_verification_commands_default_to_pnpm_with_frozen_lockfile(tmp_path):
    _package(tmp_path, {"build": "next build", "test": "vitest run", "lint": "eslint"})
    assert workspace.verification_commands(tmp_path) == [
        "pnpm install --frozen-lockfile",
        "pnpm run build",
        "pnpm test",
    ]


def test_verification_uses_npm_for_projects_with_only_an_npm_lockfile(tmp_path):
    _package(tmp_path, {"build": "next build"})
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    assert workspace.verification_commands(tmp_path) == ["npm ci", "npm run build"]


def test_verification_ignores_npm_init_placeholder_test(tmp_path):
    _package(tmp_path, {"build": "tsc", "test": 'echo "Error: no test specified" && exit 1'})
    assert workspace.verification_commands(tmp_path)[1:] == ["pnpm run build"]


def test_verify_fails_node_project_without_build_or_test(tmp_path):
    _package(tmp_path, {})
    ok, report = workspace.verify_project(tmp_path)
    assert not ok
    assert "neither" in report


def test_verify_runs_node_scripts(tmp_path):
    _package(tmp_path, {"build": 'node -e "process.exit(0)"', "test": 'node -e "process.exit(1)"'})
    ok, report = workspace.verify_project(tmp_path)
    assert not ok
    assert "$ pnpm test" in report


def test_agent_shell_disables_pnpm_ci_frozen_lockfile(tmp_path):
    result = workspace.run_command(tmp_path, 'node -e "console.log(process.env.npm_config_frozen_lockfile)"')
    assert result["output"].strip() == "false"


def test_verify_python_project(tmp_path):
    (tmp_path / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    ok, _ = workspace.verify_project(tmp_path)
    assert ok


def test_verify_empty_dir_fails(tmp_path):
    ok, report = workspace.verify_project(tmp_path)
    assert not ok
    assert "nothing to verify" in report


def test_lint_skipped_when_no_lint_script(tmp_path):
    _package(tmp_path, {"build": "x"})
    ok, report = workspace.lint_project(tmp_path)
    assert ok and "skipped" in report
