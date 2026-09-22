import json
import os
import pytest
from utils import state as state_mod


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_read_state_missing_file_returns_empty_dict():
    assert state_mod.read_state() == {}


def test_read_state_malformed_json_returns_empty_dict():
    with open(state_mod.STATE_FILE, "w") as f:
        f.write("{not valid json")
    assert state_mod.read_state() == {}


def test_write_then_read_state_round_trips():
    payload = {"phase": "building", "project_slug": "vxlt"}
    state_mod.write_state(payload)
    assert state_mod.read_state() == payload


def test_write_state_is_atomic_no_tmp_file_left_behind(tmp_path):
    state_mod.write_state({"phase": "planning"})
    leftover_tmp_files = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftover_tmp_files == []


def test_write_state_overwrites_existing_file():
    state_mod.write_state({"phase": "planning"})
    state_mod.write_state({"phase": "building"})
    assert state_mod.read_state() == {"phase": "building"}
