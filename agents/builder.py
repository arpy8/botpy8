import json
import subprocess
from pathlib import Path

from utils.llm import generate


def load_plan(slug: str, base_dir: Path) -> dict:
    plan_path = base_dir / "plans" / f"{slug}.json"
    return json.loads(plan_path.read_text(encoding="utf-8"))


def scaffold_files(plan: dict, project_dir: Path, client) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    project_root = project_dir.resolve()
    for rel_path in plan["structure"]:
        file_path = project_dir / rel_path
        resolved = file_path.resolve()
        if resolved != project_root and project_root not in resolved.parents:
            raise ValueError(
                f"structure entry {rel_path!r} escapes project directory {project_dir}"
            )
        if file_path.exists():
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        system = (
            "Generate the full contents of one source file for a software "
            "project. Respond with only the file contents, no explanation."
        )
        user = (
            f"Project: {plan['name']} — {plan['pitch']}\n"
            f"Tech stack: {', '.join(plan['stack'])}\n"
            f"File to generate: {rel_path}\n"
            f"Features: {', '.join(plan['features'])}"
        )
        content = generate(client, system, user)
        file_path.write_text(content, encoding="utf-8")


def generate_readme(plan: dict, project_dir: Path, client) -> None:
    readme_path = project_dir / "README.md"
    if readme_path.exists():
        return
    system = (
        "Write a project README with sections: Overview, Tech Stack, Setup, "
        "Usage, Architecture, and 'How it was built'. The last section must "
        "be the first-person build story provided, not a generic template."
    )
    user = (
        f"Project: {plan['name']} — {plan['pitch']}\n"
        f"Stack: {', '.join(plan['stack'])}\n"
        f"Features: {', '.join(plan['features'])}\n"
        f"Build story: {plan['story']}"
    )
    content = generate(client, system, user)
    readme_path.write_text(content, encoding="utf-8")


def run_smoke_check(project_dir: Path, stack: list[str]) -> bool:
    lowered = [s.lower() for s in stack]
    if any("python" in s for s in lowered):
        cmd = ["pytest", "-q"]
    elif any(s in ("node", "next.js", "javascript", "typescript") for s in lowered):
        cmd = ["npm", "test", "--silent"]
    else:
        cmd = ["make", "test"]
    result = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True)
    return result.returncode == 0


def run(base_dir: Path, client, state: dict) -> dict:
    slug = state["project_slug"]
    plan = load_plan(slug, base_dir)
    project_dir = base_dir / "projects" / slug
    scaffold_files(plan, project_dir, client)
    generate_readme(plan, project_dir, client)
    passed = run_smoke_check(project_dir, plan["stack"])
    state["phase"] = "validating" if passed else "failed"
    return state


def resume(base_dir: Path, client, state: dict) -> dict:
    return run(base_dir, client, state)
