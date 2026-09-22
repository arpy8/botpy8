.PHONY: install run dry-run test

install:
	uv sync

run:
	uv run python orchestrator.py

dry-run:
	uv run python orchestrator.py --dry-run

test:
	uv run pytest -v
