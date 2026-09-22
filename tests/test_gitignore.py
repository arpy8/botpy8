from pathlib import Path

REQUIRED_PATTERNS = {"soul.md", "plans/", "logs/", "state.json", ".env"}


def test_gitignore_covers_all_runtime_and_identity_files():
    content = Path(".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in content.splitlines()}
    missing = REQUIRED_PATTERNS - lines
    assert missing == set()
