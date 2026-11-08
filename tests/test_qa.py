import functools
import http.server
import json
import threading

import pytest

from agents import builder, qa
from tests.fake_llm import ScriptedClient, build_python_project

PLAN = {
    "name": "dots",
    "slug": "dots",
    "pitch": "click to paint cells",
    "story": "s",
    "domain": "web-apps",
    "stack": ["Next.js"],
    "structure": [],
    "features": ["click a cell to paint it"],
    "aesthetic_notes": "",
}

# A 20x10 grid of 16px cells. `buggy` reproduces glyph's bug: dividing the
# CSS-pixel offset by (cell size * devicePixelRatio).
PAGE = """<!doctype html><body style="margin:0">
<canvas id="c" style="position:absolute;left:100px;top:50px;width:320px;height:160px"></canvas>
<script>
const dpr = window.devicePixelRatio, CELL = 16, buggy = %s;
const c = document.getElementById('c');
c.width = 320 * dpr; c.height = 160 * dpr;
const ctx = c.getContext('2d'); ctx.scale(dpr, dpr);
c.addEventListener('click', e => {
  const r = c.getBoundingClientRect();
  const div = buggy ? CELL * dpr : CELL;
  const col = Math.floor((e.clientX - r.left) / div), row = Math.floor((e.clientY - r.top) / div);
  ctx.fillStyle = '#34d399'; ctx.fillRect(col * CELL, row * CELL, CELL, CELL);
});
</script></body>"""


ISSUE = {
    "title": "Brush paints away from the cursor",
    "component": "CanvasGrid",
    "steps": "click (260, 130)",
    "expected": "cell under cursor painted",
    "actual": "painted box at (180, 90)",
    "severity": "major",
}


@pytest.fixture
def served(tmp_path):
    def serve(buggy: bool) -> str:
        (tmp_path / "index.html").write_text(PAGE % ("true" if buggy else "false"), encoding="utf-8")
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    servers = []
    yield serve
    for server in servers:
        server.shutdown()


def _browser(url):
    try:
        return qa.BrowserSession(url)
    except Exception as exc:  # chromium not installed in this environment
        pytest.skip(f"playwright chromium unavailable: {exc}")


@pytest.mark.parametrize("buggy", [False, True])
def test_canvas_changes_measures_where_a_click_painted(served, buggy):
    browser = _browser(served(buggy))
    try:
        browser.open("/index.html")
        browser.canvas_mark()
        # cell (10, 5) spans x 260-276, y 130-146 on the page
        browser.click(x=268, y=138)
        changes = json.loads(browser.canvas_changes())
    finally:
        browser.close()
    box = changes["page_box"]
    if buggy:
        assert (box["x"], box["y"]) == (180, 82)  # dpr 2 halves the cell index: col 5, row 2
    else:
        assert (box["x"], box["y"], box["width"], box["height"]) == (260, 130, 16, 16)


def test_snapshot_lists_canvas_with_box_and_pixel_size(served):
    browser = _browser(served(False))
    try:
        browser.open("/index.html")
        items = json.loads(browser.snapshot())
    finally:
        browser.close()
    canvas = next(i for i in items if i["tag"] == "canvas")
    assert (canvas["x"], canvas["y"], canvas["width"]) == (100, 50, 320)
    assert canvas["pixel_width"] == 320 * qa.DEVICE_SCALE_FACTOR


def test_console_errors_capture_uncaught_exceptions(served):
    browser = _browser(served(False))
    try:
        browser.open("/index.html")
        browser.evaluate("setTimeout(() => { throw new Error('kaboom') }, 0)")
        browser.page.wait_for_timeout(100)
        assert "kaboom" in browser.console_errors()
    finally:
        browser.close()


class _FakeServer:
    url = "http://fake"

    def __init__(self, project_dir):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


class _FakeBrowser:
    def __init__(self, url):
        self.closed = False
        self.diagnostics = {}

    def screenshot_b64(self):
        return "aW1n"

    def open(self, path="/"):
        return "loaded"

    def close(self):
        self.closed = True

    def drain_dialogs(self):
        return ""

    def drain_blocked_keys(self):
        return ""


def test_run_qa_collects_reported_issues_and_attaches_screenshots(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    client = ScriptedClient([
        [("open", {"path": "/"}), ("screenshot", {})],
        [("report_issue", ISSUE)],
        [("done", {"summary": "tested painting"})],
    ])
    result = qa.run_qa(PLAN, tmp_path, client)
    assert result == qa.QAResult([ISSUE], True, "tested painting")
    second_call = client.calls[1]["messages"]
    assert second_call[-1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert client.calls[0]["model"] == qa.QA_MODEL


def test_run_qa_reports_critical_issue_when_app_does_not_start(tmp_path, monkeypatch):
    class _Broken(_FakeServer):
        def __enter__(self):
            raise RuntimeError("app server exited with code 1")

    monkeypatch.setattr(qa, "AppServer", _Broken)
    issues = qa.run_qa(PLAN, tmp_path, ScriptedClient([])).issues
    assert issues[0]["severity"] == "critical"
    assert "exited" in issues[0]["actual"]


def test_run_qa_rechecks_previously_reported_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    client = ScriptedClient([
        [("recheck", {"issue_number": 1, "status": "fixed", "evidence": "paints under cursor"})],
        [("done", {"summary": "all fixed"})],
    ])
    assert qa.run_qa(PLAN, tmp_path, client, previous_issues=[ISSUE]).issues == []
    assert "Brush paints away from the cursor" in client.calls[0]["messages"][1]["content"]


def test_old_screenshots_are_dropped_from_context():
    messages = [{"role": "user", "content": [{"type": "image_url"}]} for _ in range(5)]
    qa._drop_old_screenshots(messages)
    assert [isinstance(m["content"], list) for m in messages] == [False, False, False, True, True]


def test_blocking_ignores_minor_issues():
    minor = dict(ISSUE, severity="minor")
    assert qa.blocking([ISSUE, minor]) == [ISSUE]


def test_is_web_app_needs_a_start_script(tmp_path):
    assert not qa.is_web_app(tmp_path)
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start": "next start"}}), encoding="utf-8")
    assert qa.is_web_app(tmp_path)


def test_developer_gets_qa_bugs_and_finishes_after_qa_passes(tmp_path, monkeypatch):
    qa_results = [[ISSUE], []]
    seen_previous = []

    def fake_run_qa(plan, project_dir, client, previous_issues=None):
        seen_previous.append(previous_issues)
        return qa.QAResult(qa_results.pop(0), True, "ok")

    monkeypatch.setattr(qa, "is_web_app", lambda d: True)
    monkeypatch.setattr(qa, "run_qa", fake_run_qa)
    project_dir = tmp_path / "projects" / "dots"
    client = ScriptedClient([
        build_python_project(),
        [("finish", {"summary": "done"})],
        [("write_file", {"path": "fix.txt", "content": "fixed coordinate math"})],
        [("finish", {"summary": "fixed"})],
    ])
    assert builder.run_agent(PLAN, project_dir, client) is True
    qa_feedback = client.calls[2]["messages"][-1]["content"]
    assert "QA tested the running app" in qa_feedback
    assert "Brush paints away from the cursor" in qa_feedback
    assert seen_previous == [[], [ISSUE]]


def test_developer_fails_when_qa_keeps_finding_blocking_bugs(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "is_web_app", lambda d: True)
    monkeypatch.setattr(qa, "run_qa", lambda *a, **k: qa.QAResult([ISSUE], True, "ok"))
    project_dir = tmp_path / "projects" / "dots"
    replies = [build_python_project()] + [[("finish", {"summary": "done"})]] * builder.MAX_QA_ROUNDS
    assert builder.run_agent(PLAN, project_dir, ScriptedClient(replies)) is False


def test_minor_qa_issues_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "is_web_app", lambda d: True)
    monkeypatch.setattr(qa, "run_qa", lambda *a, **k: qa.QAResult([dict(ISSUE, severity="minor")], True, "ok"))
    project_dir = tmp_path / "projects" / "dots"
    client = ScriptedClient([build_python_project(), [("finish", {"summary": "done"})]])
    assert builder.run_agent(PLAN, project_dir, client) is True


def test_clicks_outside_the_viewport_are_refused():
    assert "outside" in qa.BrowserSession._off_screen(654, 921)
    assert qa.BrowserSession._off_screen(100, 100) is None
    assert "outside" in qa.BrowserSession._off_screen(10, 10, 1300, 10)


def test_tester_is_told_its_remaining_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    replies = [[("open", {})]] * qa.BUDGET_NOTICE_EVERY + [[("done", {"summary": "ok"})]]
    client = ScriptedClient(replies)
    qa.run_qa(PLAN, tmp_path, client)
    notice = client.calls[qa.BUDGET_NOTICE_EVERY]["messages"][-1]
    assert notice["role"] == "user" and "turns left" in notice["content"]


def test_session_that_runs_out_of_turns_is_continued_by_a_fresh_one(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    monkeypatch.setattr(qa, "MAX_QA_STEPS", 2)
    client = ScriptedClient([
        [("report_issue", ISSUE)], [("open", {"path": "/settings"})],  # session 1 runs out
        [("done", {"summary": "finished the rest"})],                   # session 2
    ])
    result = qa.run_qa(PLAN, tmp_path, client)
    assert result == qa.QAResult([ISSUE], True, "finished the rest")
    continuation = client.calls[2]["messages"][1]["content"]
    assert "ran out of turns" in continuation
    assert "Brush paints away from the cursor" in continuation
    assert "/settings" in continuation


def test_incomplete_coverage_is_reported_as_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    monkeypatch.setattr(qa, "MAX_QA_STEPS", 1)
    client = ScriptedClient([[("open", {})]] * qa.MAX_QA_SESSIONS)
    assert qa.run_qa(PLAN, tmp_path, client).complete is False


def test_only_one_interaction_runs_per_turn(tmp_path, monkeypatch):
    calls = []

    class _RecordingBrowser(_FakeBrowser):
        def press(self, keys):
            calls.append(("press", keys))
            return "pressed"

        def click(self, x=None, y=None, text=None, button="left"):
            calls.append(("click", text))
            return "clicked"

    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _RecordingBrowser)
    client = ScriptedClient([
        [("press", {"keys": "Meta+k"}), ("click", {"text": "Commands"}), ("screenshot", {})],
        [("done", {"summary": "ok"})],
    ])
    qa.run_qa(PLAN, tmp_path, client)
    assert calls == [("press", "Meta+k")]
    tool_results = [m["content"] for m in client.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_results[1].startswith("skipped: only one interaction")
    assert tool_results[2] == "screenshot attached in the next message"


def test_done_is_refused_until_every_known_issue_has_a_verdict(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    client = ScriptedClient([
        [("done", {"summary": "looks fine"})],
        [("recheck", {"issue_number": 1, "status": "still_broken", "evidence": "Meta+k: palette stays closed"})],
        [("done", {"summary": "rechecked"})],
    ])
    result = qa.run_qa(PLAN, tmp_path, client, previous_issues=[ISSUE])
    assert client.calls[1]["messages"][-1]["content"].startswith("refused: re-test known issues [1]")
    assert result.complete is True
    assert [i["title"] for i in result.issues] == [ISSUE["title"]]
    assert "Meta+k: palette stays closed" in result.issues[0]["actual"]


def test_known_issues_nobody_rechecked_stay_open(tmp_path, monkeypatch):
    monkeypatch.setattr(qa, "AppServer", _FakeServer)
    monkeypatch.setattr(qa, "BrowserSession", _FakeBrowser)
    monkeypatch.setattr(qa, "MAX_QA_STEPS", 1)
    client = ScriptedClient([[("open", {})]] * qa.MAX_QA_SESSIONS)
    result = qa.run_qa(PLAN, tmp_path, client, previous_issues=[ISSUE])
    assert result.complete is False
    assert "not re-checked" in result.issues[0]["actual"]


def test_recheck_validates_its_arguments():
    verdicts = {}
    assert qa._recheck({"issue_number": 3, "status": "fixed"}, [ISSUE], verdicts).startswith("error")
    assert qa._recheck({"issue_number": 1, "status": "maybe"}, [ISSUE], verdicts).startswith("error")
    assert verdicts == {}


def test_known_issues_round_trip_through_disk(tmp_path):
    path = tmp_path / "plans" / "dots.qa.json"
    assert qa.load_known_issues(path) == []
    qa.save_known_issues(path, [ISSUE])
    assert qa.load_known_issues(path) == [ISSUE]


def test_developer_starts_with_known_issues_for_qa_to_recheck(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(qa, "is_web_app", lambda d: True)
    monkeypatch.setattr(qa, "run_qa", lambda plan, d, c, previous_issues=None: (
        seen.append(previous_issues) or qa.QAResult([], True, "ok")))
    project_dir = tmp_path / "projects" / "dots"
    client = ScriptedClient([build_python_project(), [("finish", {"summary": "done"})]])
    assert builder.run_agent(PLAN, project_dir, client, known_issues=[ISSUE]) is True
    assert seen == [[ISSUE]]


def test_evaluate_times_out_instead_of_hanging(served, monkeypatch):
    monkeypatch.setattr(qa, "EVAL_TIMEOUT_MS", 300)
    browser = _browser(served(False))
    try:
        browser.open("/index.html")
        with pytest.raises(Exception, match="timed out"):
            browser.evaluate("new Promise(() => {})")
        assert browser.evaluate("1 + 1") == "2"
        assert browser.evaluate("(async () => { await null; return 'ok' })()") == '"ok"'
    finally:
        browser.close()


SAVE_PAGE = """<!doctype html><body>
<button id="save" onclick="save()">Save</button>
<script>
function save() {
  const name = prompt('Project name:', '');
  if (!name) return;
  localStorage.setItem('last', name);
  const r = indexedDB.open('appdb', 1);
  r.onupgradeneeded = () => r.result.createObjectStore('projects');
  r.onsuccess = () => { r.result.transaction('projects', 'readwrite').objectStore('projects').put({}, name); };
}
</script></body>"""


@pytest.fixture
def save_page(tmp_path):
    (tmp_path / "save.html").write_text(SAVE_PAGE, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_prompts_are_answered_like_a_user_and_reported(save_page):
    browser = _browser(save_page)
    try:
        browser.open("/save.html")
        storage_before = json.loads(browser.storage())
        browser.click(text="Save")
        browser.page.wait_for_timeout(300)
        note = browser.drain_dialogs()
        storage = json.loads(browser.storage())
    finally:
        browser.close()
    assert storage_before["indexedDB"] == {}  # inspecting storage must not create databases
    assert "prompt 'Project name:' answered with 'QA Test'" in note
    assert storage["localStorage"] == {"last": "QA Test"}
    assert storage["indexedDB"]["appdb"]["stores"]["projects"]["keys"] == ["QA Test"]


SHORTCUT_PAGE = """<!doctype html><body>
<input id="fps" type="number" value="12">
<script>
window.addEventListener('keydown', e => {
  if (/^[0-9]$/.test(e.key)) { window.lastColor = e.key; e.preventDefault(); }
});
</script></body>"""


def test_keys_swallowed_by_a_global_shortcut_handler_are_reported(tmp_path):
    (tmp_path / "keys.html").write_text(SHORTCUT_PAGE, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = _browser(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        browser.open("/keys.html")
        browser.page.click("#fps", click_count=3)
        browser.type_text("24")
        value = browser.page.input_value("#fps")
        note = browser.drain_blocked_keys()
        browser.type_text("x")
        assert browser.drain_blocked_keys() == ""  # unblocked keys are not reported
    finally:
        browser.close()
        server.shutdown()
    assert value == "12"
    assert "keys ['2', '4'] typed into input[type=number] were cancelled" in note
    (diagnostic,) = browser.diagnostics.values()
    assert diagnostic.startswith("Typing into input[type=number] fields is blocked")
    assert "listener on window" in note and "preventDefault" in note


def test_harness_diagnostics_reach_the_developer_even_if_the_tester_omits_them(tmp_path, monkeypatch):
    diag = "Typing into input[type=number] fields is blocked: a keydown listener on window calls preventDefault()"
    rounds = [qa.QAResult([ISSUE], True, "found it", [diag]), qa.QAResult([], True, "ok")]
    monkeypatch.setattr(qa, "is_web_app", lambda d: True)
    monkeypatch.setattr(qa, "run_qa", lambda *a, **k: rounds.pop(0))
    project_dir = tmp_path / "projects" / "dots"
    client = ScriptedClient([
        build_python_project(),
        [("finish", {"summary": "done"})],
        [("finish", {"summary": "fixed"})],
    ])
    assert builder.run_agent(PLAN, project_dir, client) is True
    feedback = client.calls[2]["messages"][-1]["content"]
    assert "Automatic diagnostics recorded by the QA browser" in feedback
    assert diag in feedback


def test_finishes_that_pass_checks_do_not_use_up_failed_finish_attempts(tmp_path, monkeypatch):
    rounds = [qa.QAResult([ISSUE], True, "bug")] * (builder.MAX_FINISH_ATTEMPTS - 1) + [qa.QAResult([], True, "ok")]
    monkeypatch.setattr(builder, "MAX_QA_ROUNDS", builder.MAX_FINISH_ATTEMPTS + 2)
    monkeypatch.setattr(qa, "is_web_app", lambda d: True)
    monkeypatch.setattr(qa, "run_qa", lambda *a, **k: rounds.pop(0))
    project_dir = tmp_path / "projects" / "dots"
    replies = ([build_python_project(), [("write_file", {"path": "README.md", "content": "no story"})]]
               + [[("finish", {"summary": "x"})]]  # fails acceptance once (README)
               + [build_python_project()]
               + [[("finish", {"summary": "x"})]] * builder.MAX_FINISH_ATTEMPTS)
    assert builder.run_agent(PLAN, project_dir, ScriptedClient(replies)) is True
