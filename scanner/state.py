"""scanner/state.py -- load/save the seen-listings state file."""
import json
from scanner.config import log, STATE_FILE, TODAY


def load_state() -> dict:
    data = json.loads(STATE_FILE.read_text())
    data.setdefault("listings", {})
    data.setdefault("meta", {})
    return data


def save_state(state: dict) -> None:
    state["meta"]["last_run"]  = TODAY
    state["meta"]["run_count"] = state["meta"].get("run_count", 0) + 1
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    log.info("State saved -> %s  (%d listings)", STATE_FILE.name, len(state["listings"]))
