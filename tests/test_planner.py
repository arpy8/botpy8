import json
import pytest
from agents import planner


class _FakeCompletions:
    def __init__(self, contents):
        self._contents = list(contents)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents.pop(0)
        return _FakeResponse(content)


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
    def __init__(self, contents):
        self.completions = _FakeCompletions(contents)


class _FakeClient:
    def __init__(self, contents):
        self.chat = _FakeChat(contents)


PLAN_JSON = json.dumps({
    "name": "wylt",
    "slug": "wylt",
    "pitch": "a clipboard history CLI",
    "story": "built this because I kept losing copied snippets",
    "domain": "cli-tools",
    "stack": ["python"],
    "structure": ["main.py", "README.md"],
    "features": ["clipboard history", "fuzzy search"],
    "aesthetic_notes": "minimal terminal output",
})


def test_normalize_structure_flattens_indented_tree_with_comments():
    # Exactly the shape that broke the glyph build.
    tree = [
        "src/",
        "  components/",
        "    CanvasGrid.tsx        // 80x24+ character grid",
        "    Timeline.tsx          // Frame timeline",
        "  hooks/",
        "    useProject.ts         // IndexedDB CRUD",
        "  App.tsx",
        "public/",
        "  sw.js",
        "package.json",
        "smoke.test.js",
    ]
    assert planner.normalize_structure(tree) == [
        "src/components/CanvasGrid.tsx",
        "src/components/Timeline.tsx",
        "src/hooks/useProject.ts",
        "src/App.tsx",
        "public/sw.js",
        "package.json",
        "smoke.test.js",
    ]


def test_normalize_structure_keeps_flat_paths_and_handles_tree_glyphs():
    assert planner.normalize_structure(["src/app/page.tsx", "./README.md"]) == [
        "src/app/page.tsx",
        "README.md",
    ]
    assert planner.normalize_structure(["src/", "├── lib/", "│   └── a.ts  # helper", "└── b.ts"]) == [
        "src/lib/a.ts",
        "src/b.ts",
    ]


def test_read_soul_raises_when_missing(tmp_path):
    with pytest.raises(FileNotFoundError, match="soul.md"):
        planner.read_soul(tmp_path)


def test_read_soul_returns_contents(tmp_path):
    (tmp_path / "soul.md").write_text("developer profile text", encoding="utf-8")
    assert planner.read_soul(tmp_path) == "developer profile text"


def test_read_projects_registry_missing_file_returns_empty_list(tmp_path):
    assert planner.read_projects_registry(tmp_path) == []


def test_read_projects_registry_returns_parsed_list(tmp_path):
    (tmp_path / "projects.json").write_text(
        json.dumps([{"name": "wylt", "slug": "wylt", "pitch": "x"}]), encoding="utf-8"
    )
    result = planner.read_projects_registry(tmp_path)
    assert result == [{"name": "wylt", "slug": "wylt", "pitch": "x"}]


def test_pick_domain_never_returns_last_domain():
    for _ in range(20):
        assert planner.pick_domain("cli-tools") != "cli-tools"


def test_pick_domain_handles_no_last_domain():
    assert planner.pick_domain(None) in planner.DOMAINS


def test_slug_exists_true_when_project_dir_present(tmp_path):
    (tmp_path / "projects" / "wylt").mkdir(parents=True)
    assert planner.slug_exists("wylt", tmp_path) is True


def test_slug_exists_false_when_absent(tmp_path):
    assert planner.slug_exists("wylt", tmp_path) is False


def test_generate_plan_parses_llm_json_and_sets_domain():
    fake = _FakeClient([PLAN_JSON])
    plan = planner.generate_plan(fake, soul_text="profile", prior_projects=[], domain="cli-tools")
    assert plan["slug"] == "wylt"
    assert plan["domain"] == "cli-tools"


def test_write_plan_creates_file_under_plans_dir(tmp_path):
    plan = json.loads(PLAN_JSON)
    plan["domain"] = "cli-tools"
    path = planner.write_plan(plan, tmp_path)
    assert path == tmp_path / "plans" / "wylt.json"
    assert json.loads(path.read_text(encoding="utf-8"))["slug"] == "wylt"


def test_run_writes_plan_and_updates_state(tmp_path):
    (tmp_path / "soul.md").write_text("profile", encoding="utf-8")
    fake = _FakeClient([PLAN_JSON])
    state = {}
    result = planner.run(tmp_path, fake, state)
    assert result["phase"] == "building"
    assert result["project_slug"] == "wylt"
    assert (tmp_path / "plans" / "wylt.json").exists()


def test_run_regenerates_on_invalid_slug(tmp_path):
    (tmp_path / "soul.md").write_text("profile", encoding="utf-8")
    bad_plan = json.loads(PLAN_JSON)
    bad_plan["slug"] = "../../etc/passwd"
    good_plan = json.loads(PLAN_JSON)
    good_plan["slug"] = "exl"
    good_plan["name"] = "exl"
    fake = _FakeClient([json.dumps(bad_plan), json.dumps(good_plan)])
    state = {}
    result = planner.run(tmp_path, fake, state)
    assert result["project_slug"] == "exl"


def test_generate_plan_strips_markdown_json_fences():
    fenced = "```json\n" + PLAN_JSON + "\n```"
    fake = _FakeClient([fenced])
    plan = planner.generate_plan(fake, soul_text="profile", prior_projects=[], domain="cli-tools")
    assert plan["slug"] == "wylt"


def test_run_ensures_structure_includes_a_smoke_test_file(tmp_path):
    (tmp_path / "soul.md").write_text("profile", encoding="utf-8")
    no_test_plan = json.loads(PLAN_JSON)
    no_test_plan["structure"] = ["main.py"]
    fake = _FakeClient([json.dumps(no_test_plan)])
    state = {}
    result = planner.run(tmp_path, fake, state)
    saved_plan = json.loads((tmp_path / "plans" / "wylt.json").read_text(encoding="utf-8"))
    assert any("test" in entry.lower() for entry in saved_plan["structure"])


def test_run_regenerates_on_slug_collision(tmp_path):
    (tmp_path / "soul.md").write_text("profile", encoding="utf-8")
    (tmp_path / "projects" / "wylt").mkdir(parents=True)
    other_plan = json.loads(PLAN_JSON)
    other_plan["slug"] = "exl"
    other_plan["name"] = "exl"
    fake = _FakeClient([PLAN_JSON, json.dumps(other_plan)])
    state = {}
    result = planner.run(tmp_path, fake, state)
    assert result["project_slug"] == "exl"
