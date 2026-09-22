import json
import os
import tempfile

STATE_FILE = "state.json"


def read_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    """Atomic write — crash during write never corrupts STATE_FILE."""
    dir_ = os.path.dirname(os.path.abspath(STATE_FILE))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as tf:
        json.dump(state, tf, indent=2)
        tmp_path = tf.name
    os.replace(tmp_path, STATE_FILE)
