"""Read-only check against the REAL FCC and ALS endpoints (needs network access to them).

Separate from test_agent.py, which is keyless and fully local. This one only does GETs.
Usage:  python test_real_endpoints.py
"""
import asyncio
import os
import sys

os.environ["UPC_BACKEND"] = "real"   # must be set before config is imported

import config  # noqa: E402
from upc_agent import tools  # noqa: E402

# Example from the real systems: product 28399242 (inactive) has UPC 492043049380.
PRODUCT_ID, UPC, ENV = "28399242", "492043049380", "mcore-012"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


async def main() -> None:
    print(f"\n=== Real endpoints (UPC_LENGTH={config.UPC_LENGTH}) ===")
    check("list_environments", (await tools.list_environments())["environments"] == [ENV])

    r = await tools.resolve_upc(ENV, PRODUCT_ID)
    check("FCC product -> UPCs", r["status"] == "success" and UPC in r["upcs"], str(r.get("upcs")))
    r = await tools.resolve_upc(ENV, "1")
    check("FCC unknown product", r["status"] == "not_found", r.get("error_message", "")[:60])

    r = await tools.check_code_status(PRODUCT_ID)
    row = r["upcs"][0] if r["status"] == "success" else {}
    check("check_code_status product ID", r["status"] == "success" and row.get("upc") == UPC
          and row.get("available") is False, f"{row.get('inventory_status')}: {row.get('reason')}")

    check("check_code_status includes FCC product", r["product"]["id"] == int(PRODUCT_ID)
          and r["product"]["active"] is False, str(r["product"]["name"]))

    r = await tools.check_code_status(UPC)
    check("check_code_status UPC", r["status"] == "success" and r["upcs"][0]["upc"] == UPC)
    check("check_code_status unknown environment", (await tools.check_code_status(PRODUCT_ID, "nope"))["status"] == "not_found")
    check("check_environment_status unsupported", (await tools.check_environment_status(ENV))["status"] == "error")
    check("send_upc_to_als blocked", (await tools.send_upc_to_als(UPC, ENV, "x", UPC))["status"] == "error")


asyncio.run(main())
print(f"\n{'All checks passed.' if not failures else f'{len(failures)} FAILED: {failures}'}")
sys.exit(1 if failures else 0)
