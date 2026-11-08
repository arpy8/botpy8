"""QA: a tool-using tester that runs the built app in a real browser.

The developer agent's checks (build, unit tests, lint) prove the code
compiles; they say nothing about whether clicking the canvas paints under
the cursor. This agent starts the production build, drives it with
Playwright, exercises every feature in the plan, and files concrete bug
reports — steps, expected, actual, measured evidence — that go back to the
developer agent to fix.

Run it standalone against an existing project:

    uv run python -m agents.qa projects/glyph          # report bugs
    uv run python -m agents.qa projects/glyph --fix    # report, then have the developer fix them
"""

import argparse
import base64
import json
import logging
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

from agents import workspace
from utils.llm import assistant_message_dict, chat

QA_MODEL = os.environ.get("BOTPY8_SOUL_QA_MODEL", "nvidia/nemotron-3.5-super-vl:free")
MAX_QA_STEPS = 120
BUDGET_NOTICE_EVERY = 15
MAX_QA_SESSIONS = 2
MAX_TEXT_ONLY_REPLIES = 3
KEEP_SCREENSHOTS = 2
SERVER_START_TIMEOUT = 120
VIEWPORT = {"width": 1280, "height": 800}
# Most laptop and Windows-scaled screens are >1. Bugs that mix CSS pixels
# with device pixels are invisible at 1, so test where users actually are.
DEVICE_SCALE_FACTOR = 2
BLOCKING_SEVERITIES = {"critical", "major"}
INTERACTIONS = {"open", "click", "drag", "press", "type_text"}
EVAL_TIMEOUT_MS = 10000
DIALOG_PROMPT_ANSWER = "QA Test"

TOOLS = [
    {"type": "function", "function": {
        "name": "open",
        "description": "Load a page of the app (path relative to the app root, default '/'). Clears collected console errors.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "screenshot",
        "description": f"Look at the page. Coordinates in the image are CSS pixels (viewport {VIEWPORT['width']}x{VIEWPORT['height']}).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "snapshot",
        "description": (
            "List visible interactive elements and canvases with their text/labels and bounding boxes "
            "(CSS pixels: x, y, width, height). Canvases also report their internal pixel size."
        ),
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "click",
        "description": "Click at CSS-pixel coordinates, or on the element whose visible text or aria-label matches `text`.",
        "parameters": {"type": "object", "properties": {
            "x": {"type": "number"}, "y": {"type": "number"},
            "text": {"type": "string"},
            "button": {"type": "string", "enum": ["left", "right"]},
        }},
    }},
    {"type": "function", "function": {
        "name": "drag",
        "description": "Press the left mouse button at (from_x, from_y), move in steps to (to_x, to_y), release.",
        "parameters": {"type": "object", "properties": {
            "from_x": {"type": "number"}, "from_y": {"type": "number"},
            "to_x": {"type": "number"}, "to_y": {"type": "number"},
        }, "required": ["from_x", "from_y", "to_x", "to_y"]},
    }},
    {"type": "function", "function": {
        "name": "press",
        "description": "Press keys, e.g. 'h', 'Escape', 'Control+z', 'Meta+k'. Space-separate several presses.",
        "parameters": {"type": "object", "properties": {"keys": {"type": "string"}}, "required": ["keys"]},
    }},
    {"type": "function", "function": {
        "name": "type_text",
        "description": "Type text into whatever has focus.",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    }},
    {"type": "function", "function": {
        "name": "canvas_mark",
        "description": "Remember the current pixels of a canvas (default the largest one) so canvas_changes can diff against them.",
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "canvas_changes",
        "description": (
            "Report where the canvas changed since canvas_mark: bounding box of changed pixels in page CSS "
            "pixels, plus the count. Use it to check that drawing lands exactly where you clicked."
        ),
        "parameters": {"type": "object", "properties": {"selector": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "evaluate",
        "description": "Run a JavaScript expression in the page and return its JSON-serialisable result (e.g. read localStorage, IndexedDB, DOM state).",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
    }},
    {"type": "function", "function": {
        "name": "storage",
        "description": "Show what the app has persisted: localStorage and every existing IndexedDB database (stores, key counts, keys). Use it to verify save/load.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "console_errors",
        "description": "Uncaught exceptions and console.error messages since the page was opened.",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a source file of the project (path relative to project root) to learn what a component is supposed to do.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "List the project's source files.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }},
    {"type": "function", "function": {
        "name": "recheck",
        "description": "Record whether a known bug from an earlier QA run is fixed, after re-testing it.",
        "parameters": {"type": "object", "properties": {
            "issue_number": {"type": "integer", "description": "its # in the known-bugs list"},
            "status": {"type": "string", "enum": ["fixed", "still_broken"]},
            "evidence": {"type": "string", "description": "what you did and observed"},
        }, "required": ["issue_number", "status", "evidence"]},
    }},
    {"type": "function", "function": {
        "name": "report_issue",
        "description": "File one bug you reproduced. Call once per distinct bug.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "one line, specific"},
            "component": {"type": "string", "description": "feature/UI area, and source file if you know it"},
            "steps": {"type": "string", "description": "exact actions to reproduce, with coordinates/keys"},
            "expected": {"type": "string"},
            "actual": {"type": "string", "description": "what happened, with measurements (e.g. canvas_changes box)"},
            "severity": {"type": "string", "enum": ["critical", "major", "minor"],
                         "description": "critical: app broken/crashes; major: a planned feature doesn't work; minor: cosmetic"},
        }, "required": ["title", "component", "steps", "expected", "actual", "severity"]},
    }},
    {"type": "function", "function": {
        "name": "done",
        "description": "Finish testing. Call after every feature in the plan has been exercised.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string", "description": "what you tested and how it went"}}, "required": ["summary"]},
    }},
]

SYSTEM_PROMPT = f"""You are the QA engineer for botpy8_soul. A developer agent has built a web app; its build and
unit tests pass. Your job is to find out whether the app actually works for a user, feature by feature,
and to file precise bug reports the developer can fix without guessing.

The production build is running and you drive it in a real browser (Chromium, viewport
{VIEWPORT['width']}x{VIEWPORT['height']}, devicePixelRatio {DEVICE_SCALE_FACTOR} like a typical laptop).

How to work:
1. Read the plan's features. Skim the source (list_files, read_file) only as needed to find controls.
2. open the app, take a screenshot and a snapshot, check console_errors.
3. Exercise EVERY feature in the plan like a user would: click tools, draw, type, use keyboard shortcuts,
   open dialogs, play/pause, add frames, save and reload, export. After each action, verify the result
   with a screenshot, canvas_changes, snapshot or evaluate — never assume it worked. Only one interaction
   per turn is executed. Test every keyboard shortcut on its own, from a state where its effect is not
   already visible, separately from any button that does the same thing.
4. For anything drawn on a canvas: canvas_mark, click/drag at a known point, canvas_changes, and compare
   the changed box to where you clicked. Drawing that lands away from the cursor is a major bug — report
   the click point and the measured box.
5. Check console_errors after interactions; uncaught exceptions are bugs. If a tool result says keys typed
   into a field were cancelled by a listener, include that listener and where it is attached in the bug
   report — it is the root cause the developer needs. Browser dialogs (alert/confirm/
   prompt) are answered for you like a user would, and reported in the tool result.
   To check persistence use the storage tool. Never call indexedDB.open in evaluate: opening a database
   that does not exist yet creates an empty one and breaks the app's own schema.
6. report_issue for each bug you reproduced, with exact steps and measured evidence. Do not report
   things you did not observe, style preferences, or features missing from the plan.
7. Call done when every feature has been tried. Keep calling tools until then."""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_web_app(project_dir: Path) -> bool:
    pkg_path = project_dir / "package.json"
    if not pkg_path.exists():
        return False
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return "start" in pkg.get("scripts", {})


class AppServer:
    """`<pm> start` on a free port for the duration of a QA session."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None
        # Outside the project: it must not be committed, and parallel QA runs must not collide.
        fd, name = tempfile.mkstemp(prefix="botpy8-soul-qa-server-", suffix=".log")
        os.close(fd)
        self.log_path = Path(name)

    def __enter__(self) -> "AppServer":
        env = dict(os.environ, PORT=str(self.port), NEXT_TELEMETRY_DISABLED="1")
        env.pop("OPENROUTER_API_KEY", None)
        self._log = open(self.log_path, "wb")
        pm = workspace.package_manager(self.project_dir)
        self.proc = subprocess.Popen(
            f"{pm} start", shell=True, cwd=self.project_dir, env=env,
            stdin=subprocess.DEVNULL, stdout=self._log, stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"app server exited with code {self.proc.returncode}:\n{self.log_tail()}")
            try:
                with urllib.request.urlopen(self.url, timeout=5) as resp:
                    if resp.status < 500:
                        return self
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    return self
            except OSError:
                pass
            time.sleep(1)
        self.__exit__(None, None, None)
        raise RuntimeError(f"app did not respond on {self.url} within {SERVER_START_TIMEOUT}s:\n{self.log_tail()}")

    def log_tail(self) -> str:
        self._log.flush()
        return workspace._truncate(self.log_path.read_text(encoding="utf-8", errors="replace"), 3000)

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            workspace._kill_tree(self.proc)
            self.proc.wait(timeout=30)
        self._log.close()
        try:
            self.log_path.unlink(missing_ok=True)
        except OSError:
            pass  # still held by a dying server process on Windows; it is a temp file


_SNAPSHOT_JS = """() => {
  const out = [];
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const box = el => { const r = el.getBoundingClientRect();
    return {x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height)}; };
  const sel = 'button, a[href], input, select, textarea, [role=button], [role=tab], [role=menuitem], [tabindex]:not([tabindex="-1"]), canvas';
  for (const el of document.querySelectorAll(sel)) {
    if (!visible(el)) continue;
    const item = {tag: el.tagName.toLowerCase(), ...box(el)};
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.title || el.placeholder || '').trim();
    if (label) item.label = label.slice(0, 60);
    if (el.tagName === 'CANVAS') { item.pixel_width = el.width; item.pixel_height = el.height; }
    out.push(item);
    if (out.length >= 150) break;
  }
  return out;
}"""

_PICK_CANVAS_JS = """const pick = (selector) => {
  if (selector) return document.querySelector(selector);
  let best = null, area = 0;
  for (const c of document.querySelectorAll('canvas')) {
    const r = c.getBoundingClientRect();
    if (r.width * r.height > area) { area = r.width * r.height; best = c; }
  }
  return best;
};"""

_CANVAS_MARK_JS = """(selector) => {
  """ + _PICK_CANVAS_JS + """
  const c = pick(selector);
  if (!c) return {error: 'no canvas found'};
  const ctx = c.getContext('2d');
  if (!ctx) return {error: 'canvas has no 2d context'};
  window.__qaMark = {canvas: c, data: ctx.getImageData(0, 0, c.width, c.height).data.slice()};
  return {marked: true, pixel_width: c.width, pixel_height: c.height};
}"""

# Installed before any page script: records keydown/keypress listeners that cancel a
# key typed into an editable field. "Typing into the input does nothing" is usually a
# global shortcut handler swallowing the key; this names the culprit for the developer.
_KEY_GUARD_JS = """(() => {
  window.__qaBlockedKeys = [];
  const editable = t => t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName));
  const where = o => o === window ? 'window' : o === document ? 'document'
    : (o && o.tagName ? o.tagName.toLowerCase() + (o.id ? '#' + o.id : '') : 'unknown');
  const add = EventTarget.prototype.addEventListener, remove = EventTarget.prototype.removeEventListener;
  EventTarget.prototype.addEventListener = function (type, listener, opts) {
    if ((type === 'keydown' || type === 'keypress') && typeof listener === 'function') {
      const wrapped = function (e) {
        const before = e.defaultPrevented;
        const result = listener.apply(this, arguments);
        if (!before && e.defaultPrevented && editable(e.target)) {
          window.__qaBlockedKeys.push({
            key: e.key,
            target: e.target.tagName.toLowerCase() + (e.target.type ? `[type=${e.target.type}]` : ''),
            listener_on: where(this),
            listener: String(listener).replace(/\\s+/g, ' ').slice(0, 160),
          });
        }
        return result;
      };
      listener.__qaWrapped = wrapped;
      return add.call(this, type, wrapped, opts);
    }
    return add.call(this, type, listener, opts);
  };
  EventTarget.prototype.removeEventListener = function (type, listener, opts) {
    return remove.call(this, type, (listener && listener.__qaWrapped) || listener, opts);
  };
})();"""

_STORAGE_JS = """async () => {
  const out = {localStorage: {}, indexedDB: {}};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i); out.localStorage[k] = (localStorage.getItem(k) || '').slice(0, 300);
  }
  // Only open databases that exist, at their own version: indexedDB.open(name) on a
  // missing database would create an empty one and break the app's schema upgrade.
  for (const info of (await indexedDB.databases())) {
    const db = await new Promise((res, rej) => {
      const r = indexedDB.open(info.name, info.version);
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
    const stores = {};
    for (const name of db.objectStoreNames) {
      stores[name] = await new Promise(res => {
        const tx = db.transaction(name).objectStore(name), keys = tx.getAllKeys();
        keys.onsuccess = () => res({count: keys.result.length, keys: keys.result.slice(0, 20).map(String)});
        keys.onerror = () => res({error: String(keys.error)});
      });
    }
    db.close();
    out.indexedDB[info.name] = {version: info.version, stores};
  }
  return out;
}"""

_CANVAS_CHANGES_JS = """() => {
  const m = window.__qaMark;
  if (!m) return {error: 'call canvas_mark first'};
  const c = m.canvas, ctx = c.getContext('2d');
  const now = ctx.getImageData(0, 0, c.width, c.height).data;
  if (now.length !== m.data.length) return {error: 'canvas was resized since canvas_mark'};
  let minX = Infinity, minY = Infinity, maxX = -1, maxY = -1, count = 0;
  for (let i = 0; i < now.length; i += 4) {
    if (now[i] !== m.data[i] || now[i+1] !== m.data[i+1] || now[i+2] !== m.data[i+2] || now[i+3] !== m.data[i+3]) {
      const p = i / 4, x = p % c.width, y = (p - x) / c.width;
      count++; if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y;
    }
  }
  if (!count) return {changed_pixels: 0};
  const r = c.getBoundingClientRect(), sx = r.width / c.width, sy = r.height / c.height;
  return {
    changed_pixels: count,
    page_box: {x: Math.round(r.left + minX * sx), y: Math.round(r.top + minY * sy),
               width: Math.round((maxX - minX + 1) * sx), height: Math.round((maxY - minY + 1) * sy)},
    canvas_page_box: {x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)},
  };
}"""


class BrowserSession:
    def __init__(self, base_url: str):
        from playwright.sync_api import sync_playwright

        self.base_url = base_url
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch()
        self._context = self._browser.new_context(
            viewport=VIEWPORT, device_scale_factor=DEVICE_SCALE_FACTOR
        )
        self._context.add_init_script(_KEY_GUARD_JS)
        self.page = self._context.new_page()
        self.page.set_default_timeout(15000)
        self.errors: list[str] = []
        self.dialogs: list[str] = []
        self.diagnostics: dict[tuple, str] = {}
        self.page.on("dialog", self._on_dialog)
        self.page.on("pageerror", lambda exc: self.errors.append(f"uncaught: {exc}"))
        self.page.on(
            "console",
            lambda msg: self.errors.append(f"console.error: {msg.text}") if msg.type == "error" else None,
        )

    def _on_dialog(self, dialog) -> None:
        # Playwright dismisses dialogs by default, which makes prompt() return null —
        # a user would answer it, so do the same and let the tester know.
        if dialog.type == "prompt":
            answer = dialog.default_value or DIALOG_PROMPT_ANSWER
            dialog.accept(answer)
            self.dialogs.append(f"prompt {dialog.message!r} answered with {answer!r}")
        else:
            dialog.accept()
            self.dialogs.append(f"{dialog.type} {dialog.message!r} accepted")

    def drain_blocked_keys(self) -> str:
        try:
            blocked = self.page.evaluate("(window.__qaBlockedKeys || []).splice(0)")
        except Exception:  # page navigating; nothing to report
            return ""
        if not blocked:
            return ""
        by_listener: dict[tuple, list[str]] = {}
        for b in blocked:
            by_listener.setdefault((b["target"], b["listener_on"], b["listener"]), []).append(b["key"])
        notes = []
        for (target, on, listener), keys in by_listener.items():
            notes.append(f"keys {keys} typed into {target} were cancelled with preventDefault() by a keydown "
                         f"listener on {on}: {listener}")
            # Kept per listener for the developer, independent of whether the tester repeats it.
            self.diagnostics.setdefault(
                (target, on, listener),
                f"Typing into {target} fields is blocked: a keydown listener on {on} calls preventDefault() "
                f"for keys typed into it (seen: {', '.join(sorted(set(keys)))}). Listener source: {listener}",
            )
        return "".join(f"\n({n})" for n in notes)

    def drain_dialogs(self) -> str:
        shown, self.dialogs = self.dialogs, []
        return "".join(f"\n(browser dialog: {d})" for d in shown)

    def storage(self) -> str:
        return workspace._truncate(json.dumps(self.page.evaluate(_STORAGE_JS), default=str), 6000)

    def close(self) -> None:
        self._browser.close()
        self._pw.stop()

    def open(self, path: str = "/") -> str:
        self.errors.clear()
        resp = self.page.goto(self.base_url + "/" + path.lstrip("/"), wait_until="networkidle")
        return f"loaded {self.page.url} (HTTP {resp.status if resp else '?'}), title {self.page.title()!r}"

    def screenshot_b64(self) -> str:
        png = self.page.screenshot(type="jpeg", quality=70, scale="css")
        return base64.b64encode(png).decode("ascii")

    def snapshot(self) -> str:
        return json.dumps(self.page.evaluate(_SNAPSHOT_JS))

    def click(self, x=None, y=None, text=None, button="left") -> str:
        if text:
            target = self.page.get_by_text(text, exact=False).or_(self.page.get_by_label(text)).first
            target.click(button=button)
            return f"clicked element matching {text!r}"
        if x is None or y is None:
            return "error: give x and y, or text"
        off = self._off_screen(x, y)
        if off:
            return off
        self.page.mouse.click(x, y, button=button)
        return f"clicked ({x}, {y})"

    @staticmethod
    def _off_screen(*coords) -> str | None:
        xs, ys = coords[0::2], coords[1::2]
        if all(0 <= x < VIEWPORT["width"] for x in xs) and all(0 <= y < VIEWPORT["height"] for y in ys):
            return None
        return (f"error: {coords} is outside the {VIEWPORT['width']}x{VIEWPORT['height']} viewport; "
                "take a snapshot for current element positions")

    def drag(self, from_x, from_y, to_x, to_y) -> str:
        off = self._off_screen(from_x, from_y, to_x, to_y)
        if off:
            return off
        mouse = self.page.mouse
        mouse.move(from_x, from_y)
        mouse.down()
        mouse.move(to_x, to_y, steps=12)
        mouse.up()
        return f"dragged ({from_x}, {from_y}) -> ({to_x}, {to_y})"

    def press(self, keys: str) -> str:
        for key in keys.split():
            self.page.keyboard.press(key)
        return f"pressed {keys}"

    def type_text(self, text: str) -> str:
        self.page.keyboard.type(text, delay=20)
        return f"typed {text!r}"

    def canvas_mark(self, selector=None) -> str:
        return json.dumps(self.page.evaluate(_CANVAS_MARK_JS, selector))

    def canvas_changes(self, selector=None) -> str:
        self.page.wait_for_timeout(150)  # let a requestAnimationFrame render land
        return json.dumps(self.page.evaluate(_CANVAS_CHANGES_JS))

    def evaluate(self, expression: str) -> str:
        # page.evaluate has no timeout of its own: an expression awaiting a promise
        # that never settles (an IndexedDB request nobody resolves) would hang QA.
        result = self.page.evaluate(
            """async (src) => {
                const timeout = new Promise((_, reject) =>
                    setTimeout(() => reject(new Error(`evaluate timed out after ${EVAL_TIMEOUT_MS}ms`)), EVAL_TIMEOUT_MS));
                return await Promise.race([(0, eval)(`(async () => (${src}))()`), timeout]);
            }""".replace("EVAL_TIMEOUT_MS", str(EVAL_TIMEOUT_MS)),
            expression,
        )
        return workspace._truncate(json.dumps(result, default=str), 4000)

    def console_errors(self) -> str:
        return "\n".join(self.errors[-30:]) or "(none)"


def _dispatch(name: str, args: dict, browser: BrowserSession, project_dir: Path) -> str:
    if name == "open":
        return browser.open(args.get("path") or "/")
    if name == "snapshot":
        return browser.snapshot()
    if name == "click":
        return browser.click(args.get("x"), args.get("y"), args.get("text"), args.get("button") or "left")
    if name == "drag":
        return browser.drag(args["from_x"], args["from_y"], args["to_x"], args["to_y"])
    if name == "press":
        return browser.press(args["keys"])
    if name == "type_text":
        return browser.type_text(args["text"])
    if name == "canvas_mark":
        return browser.canvas_mark(args.get("selector"))
    if name == "canvas_changes":
        return browser.canvas_changes(args.get("selector"))
    if name == "evaluate":
        return browser.evaluate(args["expression"])
    if name == "console_errors":
        return browser.console_errors()
    if name == "storage":
        return browser.storage()
    if name == "read_file":
        return workspace.read_file(project_dir, args["path"])
    if name == "list_files":
        return workspace.list_files(project_dir, args.get("path") or ".")
    return f"error: unknown tool {name!r}"


def _drop_old_screenshots(messages: list[dict]) -> None:
    image_msgs = [m for m in messages if m["role"] == "user" and isinstance(m["content"], list)]
    for m in image_msgs[:-KEEP_SCREENSHOTS]:
        m["content"] = "[older screenshot removed]"


def _initial_message(plan: dict, previous_issues: list[dict] | None) -> str:
    parts = [
        f"App: {plan['name']} — {plan['pitch']}\n\nFeatures to test:\n"
        + "\n".join(f"- {f}" for f in plan.get("features", [])),
        f"Source files:\n{{files}}",
    ]
    if previous_issues:
        parts.append(
            "Known bugs from earlier QA runs — the developer may have fixed some. Before anything else, "
            "re-test each one on its own, exactly as its steps say, and record a verdict with recheck "
            "(done is refused until every one has a verdict). A still_broken verdict already sends the bug "
            "back to the developer, so do not also file it with report_issue. Then test everything else:\n"
            + format_issues(previous_issues)
        )
    return "\n\n".join(parts)


class QAResult(NamedTuple):
    issues: list[dict]
    complete: bool  # the tester called done, i.e. every feature was tried
    summary: str
    # Root causes the harness observed directly (not via the tester's reports).
    diagnostics: list[str] = []


def _recheck(args: dict, known: list[dict], verdicts: dict[int, dict]) -> str:
    try:
        number = int(args.get("issue_number", 0))
    except (TypeError, ValueError):
        number = 0
    if not 1 <= number <= len(known):
        return f"error: issue_number must be between 1 and {len(known)}"
    status = args.get("status")
    if status not in ("fixed", "still_broken"):
        return "error: status must be 'fixed' or 'still_broken'"
    verdicts[number] = {"status": status, "evidence": str(args.get("evidence", ""))}
    logging.info("[qa] recheck #%d %s: %s", number, status, known[number - 1].get("title"))
    return f"recorded verdict for known issue #{number}: {status}"


def _reopened(known: list[dict], verdicts: dict[int, dict]) -> list[dict]:
    """Known issues that are still broken, or that nobody re-checked (stay open to be safe)."""
    still_open = []
    for n, issue in enumerate(known, 1):
        verdict = verdicts.get(n)
        if verdict is None:
            still_open.append(dict(issue, actual=f"{issue.get('actual', '')} [not re-checked by QA]"))
        elif verdict["status"] == "still_broken":
            still_open.append(dict(issue, actual=f"{issue.get('actual', '')}\n   Re-checked: {verdict['evidence']}"))
    return still_open


def _session(messages: list[dict], browser: "BrowserSession", project_dir: Path, client, model: str,
             issues: list[dict], actions: list[str], known: list[dict],
             verdicts: dict[int, dict]) -> str | None:
    """One tester conversation. Returns its done-summary, or None if it ran out of turns."""
    text_only = 0
    for step in range(MAX_QA_STEPS):
        _drop_old_screenshots(messages)
        left = MAX_QA_STEPS - step
        if step and step % BUDGET_NOTICE_EVERY == 0:
            wrap = ("finish the remaining features and call done." if left > 20
                    else "then call done — list untested features in its summary.")
            messages.append({"role": "user", "content":
                             f"[{left} turns left. Report every bug you have reproduced so far now; {wrap}]"})
        message = chat(client, messages, TOOLS, model=model)
        messages.append(assistant_message_dict(message))
        if not message.tool_calls:
            text_only += 1
            if text_only > MAX_TEXT_ONLY_REPLIES:
                logging.warning("[qa] tester stopped calling tools")
                return None
            messages.append({"role": "user", "content": "Keep testing with the tools; call done when finished."})
            continue
        text_only = 0
        pending_images = []
        interacted = False
        for call in message.tool_calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": f"error: arguments were not valid JSON ({exc})"})
                continue
            logging.info("[qa] step %d: %s %s", step, name, json.dumps(args)[:120])
            if name == "done":
                missing = [n for n in range(1, len(known) + 1) if n not in verdicts]
                if missing:
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": (
                        f"refused: re-test known issues {missing} and record a recheck verdict for each "
                        "before calling done.")})
                    continue
                logging.info("[qa] done: %s", args.get("summary", ""))
                return str(args.get("summary", ""))
            if name == "recheck":
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": _recheck(args, known, verdicts)})
                continue
            if name in INTERACTIONS and interacted:
                # Batched actions blur cause and effect: a shortcut press followed by
                # a click on the same feature's button "verifies" the shortcut.
                messages.append({"role": "tool", "tool_call_id": call.id, "content": (
                    "skipped: only one interaction (open/click/drag/press/type_text) per turn. Verify the "
                    "previous one's effect first, then send this again.")})
                continue
            interacted = interacted or name in INTERACTIONS
            actions.append(f"{name} {json.dumps(args)[:100]}")
            if name == "report_issue":
                issues.append({k: str(args.get(k, "")) for k in
                               ("title", "component", "steps", "expected", "actual", "severity")})
                logging.info("[qa] issue (%s): %s", args.get("severity"), args.get("title"))
                result = f"recorded issue #{len(issues)}"
            elif name == "screenshot":
                pending_images.append(browser.screenshot_b64())
                result = "screenshot attached in the next message"
            else:
                try:
                    result = _dispatch(name, args, browser, project_dir)
                except Exception as exc:  # browser errors are information for the tester
                    result = f"error: {type(exc).__name__}: {str(exc)[:500]}"
                if name in INTERACTIONS:
                    result += browser.drain_dialogs() + browser.drain_blocked_keys()
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
        for img in pending_images:
            messages.append({"role": "user", "content": [
                {"type": "text", "text": "Screenshot of the current page:"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img}"}},
            ]})
    logging.warning("[qa] session ended without calling done after %d turns", MAX_QA_STEPS)
    return None


def run_qa(plan: dict, project_dir: Path, client, previous_issues: list[dict] | None = None,
           model: str = QA_MODEL) -> QAResult:
    """Exercise the running app and collect the bugs found.

    A session that runs out of turns hasn't tested everything, so a fresh
    session continues from its action log — "no bugs reported" only counts as
    a pass once a tester has actually called done.
    """
    try:
        server = AppServer(project_dir).__enter__()
    except RuntimeError as exc:
        return QAResult([{
            "title": "App does not start with the start script",
            "component": "package.json start script / server",
            "steps": f"{workspace.package_manager(project_dir)} start",
            "expected": "server responds on its port",
            "actual": str(exc),
            "severity": "critical",
        }], True, "app did not start")
    browser = None
    issues: list[dict] = []
    actions: list[str] = []
    known = list(previous_issues or [])
    verdicts: dict[int, dict] = {}
    try:
        browser = BrowserSession(server.url)
        files = workspace.list_files(project_dir)
        for session in range(MAX_QA_SESSIONS):
            first = _initial_message(plan, known).replace("{files}", files)
            if session:
                done_checks = ", ".join(f"#{n} {v['status']}" for n, v in sorted(verdicts.items())) or "none"
                first += (
                    "\n\nA previous QA session ran out of turns before finishing. Do not repeat its work; "
                    "test the features it did not reach, spending only a few turns per feature, then call done."
                    f"\nRecheck verdicts it already recorded: {done_checks}"
                    f"\nBugs it already reported:\n{format_issues(issues) or '(none)'}"
                    "\nIts last actions:\n" + "\n".join(actions[-60:])
                )
            messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": first}]
            summary = _session(messages, browser, project_dir, client, model, issues, actions,
                               known, verdicts)
            if summary is not None:
                return QAResult(_reopened(known, verdicts) + issues, True, summary,
                                list(browser.diagnostics.values()))
        return QAResult(_reopened(known, verdicts) + issues, False,
                        f"ran out of turns in {MAX_QA_SESSIONS} sessions", list(browser.diagnostics.values()))
    finally:
        if browser:
            browser.close()
        server.__exit__(None, None, None)


def format_diagnostics(diagnostics: list[str]) -> str:
    if not diagnostics:
        return ""
    return ("\n\nAutomatic diagnostics recorded by the QA browser (likely root causes):\n"
            + "\n".join(f"- {d}" for d in diagnostics))


def blocking(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i.get("severity") in BLOCKING_SEVERITIES]


def format_issues(issues: list[dict]) -> str:
    blocks = []
    for n, issue in enumerate(issues, 1):
        blocks.append(
            f"#{n} [{issue.get('severity', '?')}] {issue.get('title', '')}\n"
            f"   Component: {issue.get('component', '')}\n"
            f"   Steps: {issue.get('steps', '')}\n"
            f"   Expected: {issue.get('expected', '')}\n"
            f"   Actual: {issue.get('actual', '')}"
        )
    return "\n\n".join(blocks)


def main() -> None:
    from dotenv import load_dotenv

    from agents import builder
    from utils.llm import get_client

    parser = argparse.ArgumentParser(description="Run the QA agent against a built project.")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--fix", action="store_true",
                        help="hand the bugs to the developer agent and loop until QA passes")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent.parent
    load_dotenv(base_dir / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    project_dir = args.project_dir.resolve()
    plan = builder.load_plan(project_dir.name, base_dir)
    client = get_client()

    known_path = base_dir / "plans" / f"{project_dir.name}.qa.json"
    known = load_known_issues(known_path)
    if known:
        print(f"{len(known)} known bug(s) from earlier QA runs will be re-checked.")

    ok, report = workspace.verify_project(project_dir)
    if not ok:
        if not args.fix:
            print(f"Static checks fail; fix these before QA:\n{report}")
            raise SystemExit(1)
        findings = f"Verification failed:\n{report}"
        open_issues = known
    else:
        qa_result = run_qa(plan, project_dir, client, previous_issues=known)
        issues = qa_result.issues
        save_known_issues(known_path, blocking(issues))
        print(format_issues(issues) if issues else "No bugs found.")
        print(f"Coverage: {'complete' if qa_result.complete else 'INCOMPLETE'} — {qa_result.summary}")
        if not args.fix or not blocking(issues):
            raise SystemExit(1 if blocking(issues) else 0)
        open_issues = blocking(issues)
        findings = (f"QA tested the running app and found these bugs:\n\n{format_issues(open_issues)}"
                    + format_diagnostics(qa_result.diagnostics))

    # The developer agent fixes the findings; its finish runs verification and QA again,
    # re-checking each of these until QA confirms it fixed.
    ok = builder.run_agent(plan, project_dir, client, previous_failure=findings, known_issues=open_issues)
    if ok:
        save_known_issues(known_path, [])
    print("PASS" if ok else "FAIL")
    raise SystemExit(0 if ok else 1)


def load_known_issues(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_known_issues(path: Path, issues: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(issues, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
