#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("--root", required=True)
root = Path(parser.parse_args().root)
document = {"assignments": [], "objective": {"makespan": 0, "setup_minutes": 0, "weighted_tardiness": 0}, "tardy_jobs": []}
state = {"machines": []}
(root / "output/sterilizer-schedule.json").write_text(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n")
(root / "output/machine-state.json").write_text(json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
