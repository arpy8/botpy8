import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from utils.state import read_state, write_state
from utils.llm import get_client
from agents import planner, builder, validator_publisher

ACTIVE_PHASES = {"planning", "building", "validating", "publishing"}


def setup_logging(base_dir: Path) -> None:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    log_path = logs_dir / f"{timestamp}.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )


def run_cycle(base_dir: Path, dry_run: bool, client) -> dict:
    state = read_state()
    fresh_start = state.get("phase") not in ACTIVE_PHASES
    if fresh_start:
        state = {"phase": "planning", "retry_count": 0}
    else:
        logging.info(
            "[resume] Restarting from phase: %s — project: %s",
            state["phase"],
            state.get("project_slug"),
        )

    try:
        if state["phase"] == "planning":
            state = (planner.run if fresh_start else planner.resume)(base_dir, client, state)
            write_state(state)

        if state["phase"] == "building":
            state = builder.resume(base_dir, client, state)
            write_state(state)

        if state["phase"] == "validating":
            if dry_run:
                state["phase"] = "done"
            else:
                state = validator_publisher.resume(base_dir, state)
            write_state(state)

        if state["phase"] == "publishing" and not dry_run:
            state = validator_publisher.resume(base_dir, state)
            write_state(state)
    except Exception:
        logging.exception("Cycle failed")
        state["phase"] = "failed"
        write_state(state)

    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    setup_logging(base_dir)
    client = get_client()
    run_cycle(base_dir, args.dry_run, client)


if __name__ == "__main__":
    main()
