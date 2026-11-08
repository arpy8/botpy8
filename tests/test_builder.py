import json
from agents import builder
from tests.fake_llm import ScriptedClient, build_python_project, README, FAILING_TEST, PASSING_TEST

PLAN = {
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["main.py", "test_smoke.py"],
    "features": ["clipboard history"],
    "aesthetic_notes": "minimal terminal output",
}


def _write_plan(base_dir, plan=PLAN):
    plans_dir = base_dir / "plans"
    plans_dir.mkdir(exist_ok=True)
    (plans_dir / f"{plan['slug']}.json").write_text(json.dumps(plan), encoding="utf-8")


def test_load_plan_reads_json_by_slug(tmp_path):
    _write_plan(tmp_path)
    assert builder.load_plan("wylt", tmp_path) == PLAN


def test_agent_writes_files_runs_commands_and_finishes(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    client = ScriptedClient([
        [("run_command", {"command": "echo scaffolding"})],
        build_python_project(),
        [("finish", {"summary": "done"})],
    ])
    assert builder.run_agent(PLAN, project_dir, client) is True
    assert (project_dir / "test_smoke.py").read_text(encoding="utf-8") == PASSING_TEST
    # the command's output was fed back to the model
    tool_msgs = [m for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert "scaffolding" in tool_msgs[0]["content"]
    assert "exit code 0" in tool_msgs[0]["content"]


def test_agent_gets_verification_failure_and_can_fix_it(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    client = ScriptedClient([
        build_python_project(FAILING_TEST),
        [("finish", {"summary": "done"})],
        [("write_file", {"path": "test_smoke.py", "content": PASSING_TEST})],
        [("finish", {"summary": "fixed"})],
    ])
    assert builder.run_agent(PLAN, project_dir, client) is True
    feedback = client.calls[2]["messages"][-1]
    assert feedback["role"] == "tool"
    assert "verification failed" in feedback["content"]


def test_agent_rejects_finish_without_build_story_readme(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    client = ScriptedClient([
        [("write_file", {"path": "test_smoke.py", "content": PASSING_TEST})],
        [("finish", {"summary": "done"})],
        [("write_file", {"path": "README.md", "content": README})],
        [("finish", {"summary": "done"})],
    ])
    assert builder.run_agent(PLAN, project_dir, client) is True
    assert "README.md is missing" in client.calls[2]["messages"][-1]["content"]


def test_agent_tool_paths_cannot_escape_project_dir(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    client = ScriptedClient([
        [("write_file", {"path": "../../escaped.py", "content": "x"})],
        build_python_project(),
        [("finish", {"summary": "done"})],
    ])
    assert builder.run_agent(PLAN, project_dir, client) is True
    assert not (tmp_path / "escaped.py").exists()
    assert "escapes project directory" in client.calls[1]["messages"][-1]["content"]


def test_agent_nudged_when_replying_with_text_then_gives_up(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    client = ScriptedClient(["thinking..."] * (builder.MAX_TEXT_ONLY_REPLIES + 1))
    assert builder.run_agent(PLAN, project_dir, client) is False
    assert client.calls[1]["messages"][-1]["role"] == "user"


def test_agent_gives_up_after_repeated_failed_finishes(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    replies = [build_python_project(FAILING_TEST)] + [
        [("finish", {"summary": "done"})]
    ] * builder.MAX_FINISH_ATTEMPTS
    assert builder.run_agent(PLAN, project_dir, ScriptedClient(replies)) is False


def test_agent_handles_malformed_tool_arguments(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    client = ScriptedClient([[("list_files", {})], build_python_project(), [("finish", {"summary": "ok"})]])
    completions = client.chat.completions
    original = completions.create

    def create(**kwargs):
        resp = original(**kwargs)
        if len(completions.calls) == 1:
            resp.choices[0].message.tool_calls[0].function.arguments = "{not json"
        return resp

    completions.create = create
    assert builder.run_agent(PLAN, project_dir, client) is True
    assert "not valid JSON" in client.calls[1]["messages"][-1]["content"]


def test_resume_tells_agent_about_existing_files_and_last_failure(tmp_path):
    _write_plan(tmp_path)
    project_dir = tmp_path / "projects" / "wylt"
    project_dir.mkdir(parents=True)
    (project_dir / "main.py").write_text("print('hi')", encoding="utf-8")
    client = ScriptedClient([build_python_project(), [("finish", {"summary": "ok"})]])
    state = {"project_slug": "wylt", "last_failure": "lint exploded"}
    result = builder.resume(tmp_path, client, state)
    assert result["phase"] == "validating"
    assert "last_failure" not in result
    first_prompt = client.calls[0]["messages"][1]["content"]
    assert "main.py" in first_prompt
    assert "lint exploded" in first_prompt


def test_run_sets_phase_failed_when_agent_cannot_finish(tmp_path):
    _write_plan(tmp_path)
    client = ScriptedClient(["no"] * (builder.MAX_TEXT_ONLY_REPLIES + 1))
    result = builder.run(tmp_path, client, {"project_slug": "wylt"})
    assert result["phase"] == "failed"


def test_run_removes_nested_git_dir(tmp_path):
    _write_plan(tmp_path)
    project_dir = tmp_path / "projects" / "wylt"
    (project_dir / ".git").mkdir(parents=True)
    (project_dir / ".git" / "HEAD").write_text("ref", encoding="utf-8")
    client = ScriptedClient([build_python_project(), [("finish", {"summary": "ok"})]])
    builder.run(tmp_path, client, {"project_slug": "wylt"})
    assert not (project_dir / ".git").exists()


def test_check_readme_rejects_create_next_app_default(tmp_path):
    (tmp_path / "README.md").write_text(
        "This is a Next.js project bootstrapped with [`create-next-app`](...)", encoding="utf-8"
    )
    assert "create-next-app default" in builder.check_readme(tmp_path)


def test_compact_elides_old_tool_output_but_keeps_plan():
    big = "x" * 5000
    messages = [{"role": "system", "content": big}, {"role": "user", "content": big}]
    for i in range(builder.KEEP_RECENT_MESSAGES + 5):
        args = json.dumps({"path": "a.ts", "content": big})
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": str(i), "type": "function", "function": {"name": "write_file", "arguments": args}}]})
        messages.append({"role": "tool", "tool_call_id": str(i), "content": big})
    builder._compact(messages)
    assert messages[1]["content"] == big
    assert "elided" in messages[2]["tool_calls"][0]["function"]["arguments"]
    assert "elided" in messages[3]["content"]
    assert messages[-1]["content"] == big


def test_agent_warned_when_repeating_the_same_call(tmp_path):
    project_dir = tmp_path / "projects" / "wylt"
    same = [("list_files", {"path": "."})]
    client = ScriptedClient([same] * builder.REPEAT_WARNING_AFTER + [build_python_project(), [("finish", {"summary": "ok"})]])
    assert builder.run_agent(PLAN, project_dir, client) is True
    last_list_result = client.calls[builder.REPEAT_WARNING_AFTER]["messages"][-1]["content"]
    assert "exact call 3 times in a row" in last_list_result
    assert "in a row" not in client.calls[1]["messages"][-1]["content"]


def test_acceptance_rejects_disabled_type_checking(tmp_path):
    (tmp_path / "next.config.ts").write_text(
        "export default { typescript: { ignoreBuildErrors: true } };", encoding="utf-8"
    )
    assert "ignoreBuildErrors" in builder.check_no_check_bypass(tmp_path)


def test_acceptance_rejects_tailwind_not_wired_into_build(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"tailwindcss": "^4"}}), encoding="utf-8"
    )
    (tmp_path / "next.config.ts").write_text("export default { turbopack: {} };", encoding="utf-8")
    assert "not wired" in builder.check_tailwind_wired(tmp_path)
    (tmp_path / "next.config.ts").write_text(
        'export default { turbopack: { rules: { "*.css": { loaders: ["@tailwindcss/turbopack"] } } } };',
        encoding="utf-8",
    )
    assert builder.check_tailwind_wired(tmp_path) is None
