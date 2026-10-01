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

    r = await tools.check_code_status(PRODUCT_ID, ENV)
    row = r["upcs"][0] if r["status"] == "success" else {}
    check("check_code_status product ID", r["status"] == "success" and row.get("upc") == UPC
          and row.get("available") is False, f"{row.get('inventory_status')}: {row.get('reason')}")

    check("check_code_status includes FCC product", r["product"]["id"] == int(PRODUCT_ID)
          and r["product"]["active"] is False, str(r["product"]["name"]))

    r = await tools.check_code_status(UPC)
    check("check_code_status UPC", r["status"] == "success" and r["upcs"][0]["upc"] == UPC)

    print("  -- check_availability: the two entry points --")
    r = await tools.check_availability(product_id=PRODUCT_ID, environment=ENV)
    row = r["upcs"][0] if r["status"] == "success" else {}
    check("product_id + environment -> FCC -> ALS", r["status"] == "success" and row.get("upc") == UPC
          and r["product"]["name"] and r["available"] is False, f"{r['product']['name']} / {row.get('reason')}")
    r = await tools.check_availability(upc=UPC)
    check("UPC straight to ALS", r["status"] == "success" and r["product"] is None and r["upcs"][0]["upc"] == UPC
          and "note" in r and r["note"])

    print("  -- check_availability: bad input --")
    for name, kwargs in {
        "product_id without environment": dict(product_id=PRODUCT_ID),
        "neither upc nor product_id": dict(),
        "both upc and product_id": dict(upc=UPC, product_id=PRODUCT_ID, environment=ENV),
        "malformed environment": dict(product_id=PRODUCT_ID, environment="nope"),
        "environment that isn't a hostname": dict(product_id=PRODUCT_ID, environment="mcore-012/../x"),
        "UPC of wrong length": dict(upc="12345"),
        "non-numeric product_id": dict(product_id="../admin", environment=ENV),
    }.items():
        r = await tools.check_availability(**kwargs)
        check(name, r["status"] == "error", r.get("error_message", "")[:70])

    r = await tools.check_availability(product_id="1", environment=ENV)
    check("unknown product -> not_found", r["status"] == "not_found" and "no product" in r["error_message"],
          r.get("error_message", "")[:70])
    r = await tools.check_availability(product_id=PRODUCT_ID, environment="mcore-999")
    check("well-formed but nonexistent environment -> error", r["status"] == "error", r.get("error_message", "")[:90])
    r = await tools.check_availability(product_id=PRODUCT_ID, environment="mcore-011")
    print(f"  [info] mcore-011 (probe, not asserted): {r['status']} {str(r.get('error_message') or r.get('product'))[:80]}")
    check("check_environment_status unsupported", (await tools.check_environment_status(ENV))["status"] == "error")
    check("send_upc_to_als blocked", (await tools.send_upc_to_als(UPC, ENV, "x", UPC))["status"] == "error")


asyncio.run(main())
print(f"\n{'All checks passed.' if not failures else f'{len(failures)} FAILED: {failures}'}")
sys.exit(1 if failures else 0)
