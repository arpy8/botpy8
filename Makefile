.PHONY: install run loop dry-run test qa fix

install:
	uv sync
	uv run playwright install chromium

run:
	uv run python orchestrator.py

loop:
	uv run python orchestrator.py --loop

dry-run:
	uv run python orchestrator.py --dry-run

test:
	uv run pytest -v

qa:
	uv run python -m agents.qa projects/$(SLUG)

fix:
	uv run python -m agents.qa projects/$(SLUG) --fix
