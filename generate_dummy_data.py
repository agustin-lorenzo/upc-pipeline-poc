"""Generate dummy data shared by all three mock services.

Produces data/dummy_data.json containing:
  - environments: {env_name: [ {item_id, code}, ... ]}
      code is either a UPC (UPC_LENGTH digits) or a short local code (6 digits)
  - fcc_mappings: {env_name: {local_code: upc}}
      what the FCC service knows. A few local codes are deliberately left out
      so the pipeline's "FCC could not resolve" path gets exercised.
  - expected: counts the test/verification step can check against

Local codes are only unique *within* an environment: the same 6-digit code can
map to different UPCs in different environments. That's why FCC needs both.

Usage:  python generate_dummy_data.py [--seed 42] [--items-per-env 40]
"""
from __future__ import annotations
import argparse
import json
import os
import random

import config

ENVIRONMENTS = ["store-atl-001", "store-nyc-014", "dc-duluth", "web-east"]
LOCAL_CODE_LENGTH = 6


def random_digits(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789") for _ in range(n))


def generate(seed: int, items_per_env: int, local_ratio: float, unmapped_ratio: float) -> dict:
    rng = random.Random(seed)
    environments: dict[str, list[dict]] = {}
    fcc_mappings: dict[str, dict[str, str]] = {}
    expected = {"total_items": 0, "direct_upc": 0, "local_code": 0, "fcc_unresolvable": 0}

    # Shared catalog of UPCs so the same product shows up in several environments.
    catalog = [random_digits(rng, config.UPC_LENGTH) for _ in range(items_per_env * 2)]

    # Pre-pick one local code to reuse across environments (mapped to different
    # UPCs) to prove the pipeline scopes FCC lookups by environment.
    shared_local_code = random_digits(rng, LOCAL_CODE_LENGTH)

    for env in ENVIRONMENTS:
        items = []
        mapping: dict[str, str] = {}
        used_local_codes: set[str] = set()

        for i in range(items_per_env):
            item_id = f"{env}-item-{i:04d}"
            upc = rng.choice(catalog)

            if rng.random() < local_ratio:
                # Environment gives us a local code instead of the UPC.
                if i == 0:
                    local = shared_local_code
                else:
                    local = random_digits(rng, LOCAL_CODE_LENGTH)
                    while local in used_local_codes or local == shared_local_code:
                        local = random_digits(rng, LOCAL_CODE_LENGTH)
                used_local_codes.add(local)

                items.append({"item_id": item_id, "code": local})
                expected["local_code"] += 1

                if i != 0 and rng.random() < unmapped_ratio:
                    expected["fcc_unresolvable"] += 1   # FCC won't know this one
                else:
                    mapping[local] = upc
            else:
                items.append({"item_id": item_id, "code": upc})
                expected["direct_upc"] += 1

            expected["total_items"] += 1

        environments[env] = items
        fcc_mappings[env] = mapping

    return {
        "seed": seed,
        "upc_length": config.UPC_LENGTH,
        "environments": environments,
        "fcc_mappings": fcc_mappings,
        "expected": expected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--items-per-env", type=int, default=40)
    parser.add_argument("--local-ratio", type=float, default=0.4,
                        help="fraction of items that come back as local codes")
    parser.add_argument("--unmapped-ratio", type=float, default=0.1,
                        help="fraction of local codes FCC can't resolve")
    parser.add_argument("--out", default=config.DATA_FILE)
    args = parser.parse_args()

    data = generate(args.seed, args.items_per_env, args.local_ratio, args.unmapped_ratio)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(data, f, indent=2)

    e = data["expected"]
    print(f"Wrote {args.out}")
    print(f"  environments: {len(data['environments'])}   items: {e['total_items']}")
    print(f"  direct UPCs: {e['direct_upc']}   local codes: {e['local_code']} "
          f"({e['fcc_unresolvable']} unresolvable by FCC)")


if __name__ == "__main__":
    main()
