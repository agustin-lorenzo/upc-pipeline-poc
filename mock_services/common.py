from __future__ import annotations
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


def load_data() -> dict:
    if not os.path.exists(config.DATA_FILE):
        raise SystemExit(f"{config.DATA_FILE} not found. Run: python generate_dummy_data.py")
    with open(config.DATA_FILE) as f:
        return json.load(f)
