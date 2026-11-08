import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from utils.state import read_state, write_state
from utils.llm import QuotaExhausted, get_client
from utils.lock import AlreadyRunning, RunLock
from agents import planner, builder, validator_publisher

ACTIVE_PHASES = {"planning", "building", "validating", "publishing"}
LOCK_FILE = ".botpy8_soul.lock"
DEFAULT_LOOP_INTERVAL = 300  # seconds between back-to-back cycles
QUOTA_RESUME_GRACE = 120  # seconds after the provider's reset before trying again


def configure_environment(base_dir: Path) -> None:
    """Make cwd and .env consistent with base_dir before anything reads them.

    utils.state's STATE_FILE is cwd-relative by design; every agent takes
    base_dir explicitly. Without this, running `python orchestrator.py`
    from outside the repo (e.g. cron with no `cd`) reads/writes state.json
    in the wrong directory while plans/projects land under base_dir —
    silently breaking crash-safe resume.
    """
    os.chdir(base_dir)
    load_dotenv(base_dir / ".env")


def setup_logging(base_dir: Path) -> None:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
    log_path = logs_dir / f"{timestamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
        force=True,
    )


def paused_for(state: dict, now: datetime | None = None) -> float:
    """Seconds until a quota pause lifts (0 if not paused)."""
    until = state.get("paused_until")
    if not until:
        return 0.0
    now = now or datetime.now(timezone.utc)
    return max(0.0, (datetime.fromisoformat(until) - now).total_seconds())


def run_cycle(base_dir: Path, dry_run: bool, client) -> dict:
    state = read_state()
    wait = paused_for(state)
    if wait:
        logging.info("[paused] daily LLM quota exhausted; resuming %s in %.0f min",
                     state.get("project_slug") or "the pipeline", wait / 60)
        return state
    state.pop("paused_until", None)
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
    except QuotaExhausted as exc:
        # Out of requests, not out of luck: keep the project and its phase, and
        # pick it up where it stopped once the quota resets.
        resets_at = exc.resets_at or datetime.now(timezone.utc) + timedelta(hours=1)
        resume_at = resets_at + timedelta(seconds=QUOTA_RESUME_GRACE)
        state["paused_until"] = resume_at.isoformat()
        logging.warning("[paused] %s; %s stays in %s until %s", exc, state.get("project_slug"),
                        state["phase"], resume_at.strftime("%Y-%m-%d %H:%M UTC"))
        write_state(state)
    except Exception:
        logging.exception("Cycle failed")
        state["phase"] = "failed"
        write_state(state)

    return state


def loop(base_dir: Path, dry_run: bool, interval: int) -> None:
    """Build projects back to back: one cycle at a time, sleeping through quota pauses."""
    client = get_client()
    while True:
        setup_logging(base_dir)
        state = run_cycle(base_dir, dry_run, client)
        wait = paused_for(state) or interval
        logging.info("[loop] phase %s; next cycle in %.0f min", state.get("phase"), wait / 60)
        time.sleep(wait)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--loop", action="store_true", help="run cycles back to back until stopped")
    parser.add_argument("--interval", type=int, default=DEFAULT_LOOP_INTERVAL,
                        help="seconds to wait between cycles in --loop mode")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    configure_environment(base_dir)
    setup_logging(base_dir)
    try:
        with RunLock(base_dir / LOCK_FILE):
            if args.loop:
                loop(base_dir, args.dry_run, args.interval)
            state = run_cycle(base_dir, args.dry_run, get_client())
    except AlreadyRunning as exc:
        logging.info("[lock] %s; exiting", exc)
        return
    if state.get("phase") == "failed":
        sys.exit(1)


if __name__ == "__main__":
    main()
