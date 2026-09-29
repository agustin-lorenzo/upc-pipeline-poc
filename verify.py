"""Check a pipeline run against the dummy data's ground truth.

Confirms that:
  - every item from every environment got an outcome
  - direct-UPC and local-code counts match what was generated
  - every local code FCC knows was resolved to the *correct* UPC for its environment
  - unresolvable codes were reported as fcc_not_found, not silently dropped
  - ALS received exactly the items the pipeline says it sent

Usage (services must be running):  python verify.py
"""
from __future__ import annotations
import json
import os
import sys

import httpx

import config


def verify() -> bool:
    with open(config.DATA_FILE) as f:
        data = json.load(f)
    with open(os.path.join(config.OUTPUT_DIR, "results.json")) as f:
        results = json.load(f)

    expected = data["expected"]
    mappings = data["fcc_mappings"]
    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, ok, detail))

    check("all items processed", len(results) == expected["total_items"],
          f"{len(results)} vs {expected['total_items']}")
    n_upc = sum(r["code_type"] == "upc" for r in results)
    n_local = sum(r["code_type"] == "local" for r in results)
    check("direct UPC count", n_upc == expected["direct_upc"], f"{n_upc} vs {expected['direct_upc']}")
    check("local code count", n_local == expected["local_code"], f"{n_local} vs {expected['local_code']}")

    wrong = [r for r in results if r["code_type"] == "local" and r["upc"] is not None
             and mappings[r["environment"]].get(r["source_code"]) != r["upc"]]
    check("FCC resolutions correct per environment", not wrong, f"{len(wrong)} wrong")

    not_found = sum(r["outcome"] == "fcc_not_found" for r in results)
    check("unresolvable codes reported", not_found == expected["fcc_unresolvable"],
          f"{not_found} vs {expected['fcc_unresolvable']}")

    sent = {r["item_id"]: r["upc"] for r in results if r["outcome"] in ("sent_direct", "sent_via_fcc")}
    als = httpx.get(f"{config.ALS_SERVICE_URL}/als/received", timeout=10).json()
    als_map = {a["item_id"]: a["upc"] for a in als}
    check("ALS received exactly what was sent", sent == als_map, f"sent {len(sent)}, ALS has {len(als_map)}")

    print("\n=== Verification ===")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    passed = all(ok for _, ok, _ in checks)
    print("All checks passed." if passed else "Some checks FAILED.")
    return passed


if __name__ == "__main__":
    sys.exit(0 if verify() else 1)
