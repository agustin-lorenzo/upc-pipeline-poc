"""UPC pipeline: environments -> (FCC if needed) -> ALS.

For every environment:
  1. Page through all items the environment returns.
  2. Classify each code:
       - UPC_LENGTH digits      -> it's a UPC, send straight to ALS
       - anything else          -> environment-specific code, ask FCC for the UPC
  3. Send each resolved UPC to ALS.

Every item ends up in exactly one outcome bucket:
  sent_direct      UPC came from the environment, ALS accepted it
  sent_via_fcc     FCC resolved the code, ALS accepted it
  fcc_not_found    FCC returned 404 for (environment, code)
  fcc_error        FCC failed after retries
  als_rejected     ALS returned a 4xx
  als_error        ALS failed after retries
  env_error        couldn't read the environment at all (recorded per environment)

Results go to output/results.json (one row per item) and output/summary.json.

Usage:
  python pipeline.py                          # all environments
  python pipeline.py --env store-atl-001      # just one (repeatable)
  python pipeline.py --dry-run                # resolve via FCC but don't call ALS
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field

import httpx

import config

log = logging.getLogger("pipeline")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class ItemResult:
    environment: str
    item_id: str
    source_code: str
    code_type: str                 # "upc" | "local"
    upc: str | None = None
    outcome: str = "pending"
    als_id: str | None = None
    error: str | None = None


def classify(code: str) -> str:
    code = code.strip()
    return "upc" if code.isdigit() and len(code) == config.UPC_LENGTH else "local"


class PermanentError(Exception):
    """A 4xx response: retrying won't help."""
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status = status


# ---------------------------------------------------------------------------
# HTTP helper with retries on transient failures only
# ---------------------------------------------------------------------------
async def request_json(client: httpx.AsyncClient, method: str, url: str, **kwargs):
    last_exc: Exception | None = None
    for attempt in range(1, config.MAX_RETRIES + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            if 400 <= resp.status_code < 500:
                try:
                    detail = resp.json().get("detail", resp.text)
                except ValueError:
                    detail = resp.text
                raise PermanentError(resp.status_code, str(detail))
            resp.raise_for_status()
            return resp.json()
        except PermanentError:
            raise
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            if attempt < config.MAX_RETRIES:
                backoff = 0.25 * 2 ** (attempt - 1)
                log.warning("%s %s failed (%s), retry %d in %.2fs", method, url, exc, attempt, backoff)
                await asyncio.sleep(backoff)
    raise RuntimeError(f"{method} {url} failed after {config.MAX_RETRIES} attempts: {last_exc}")


# ---------------------------------------------------------------------------
# Endpoint clients
# ---------------------------------------------------------------------------
async def get_environments(client: httpx.AsyncClient) -> list[str]:
    if config.REAL:
        return list(config.REAL_ENVIRONMENTS)
    return await request_json(client, "GET", f"{config.ENV_SERVICE_URL}/environments")


async def get_all_items(client: httpx.AsyncClient, env: str) -> list[dict]:
    items, offset = [], 0
    while offset is not None:
        page = await request_json(
            client, "GET", f"{config.ENV_SERVICE_URL}/environments/{env}/items",
            params={"offset": offset, "limit": config.PAGE_SIZE},
        )
        items.extend(page["items"])
        offset = page.get("next_offset")
    return items


class FCCClient:
    """Looks up UPCs, caching per (environment, code) so repeats cost one call."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._cache: dict[tuple, asyncio.Future] = {}
        self.products: dict[tuple[str, str], dict] = {}   # real mode: FCC product summary per (env, code)
        self.calls = 0

    async def resolve(self, env: str, code: str) -> str:
        key = (env, code)
        if key not in self._cache:
            self._cache[key] = asyncio.ensure_future(self._fetch(env, code))
        return await self._cache[key]

    async def _fetch(self, env: str, code: str) -> str:
        if config.REAL:
            return (await self.resolve_all(env, code))[0]
        self.calls += 1
        data = await request_json(
            self.client, "GET", f"{config.FCC_SERVICE_URL}/fcc/upc",
            params={"environment": env, "code": code},
        )
        return data["upc"]

    async def resolve_all(self, env: str, code: str) -> list[str]:
        """Every UPC FCC has for a code. Mock FCC gives one; a real product can have several."""
        if not config.REAL:
            return [await self.resolve(env, code)]
        key = ("all", env, code)
        if key not in self._cache:
            self._cache[key] = asyncio.ensure_future(self._fetch_real(env, code))
        return await self._cache[key]

    async def _fetch_real(self, env: str, code: str) -> list[str]:
        self.calls += 1
        base = config.FCC_REAL_URL_TEMPLATE.format(environment=env)
        product = await request_json(self.client, "GET", f"{base}/api/catalog/v2/products/{code}")
        p = product.get("product") or {}
        if not p:   # FCC answers 200 {"product": {}} for IDs it doesn't have
            raise PermanentError(404, f"FCC has no product {code} in '{env}'")
        self.products[(env, code)] = {k: p.get(k) for k in ("id", "name", "typeName", "active", "live", "available")}
        upcs = extract_upcs(product)
        if not upcs:
            raise PermanentError(404, f"FCC product {code} ('{p.get('name')}') in '{env}' exists but has no "
                                      f"{config.UPC_LENGTH}-digit UPCs in the fields we read")
        return upcs


def extract_upcs(node, _under_upc: bool = False) -> list[str]:
    """Collects UPC_LENGTH-digit values stored under any key containing 'upc'.

    The real FCC response format for UPCs is only partly known: the one example seen
    had them in "unavailableUpcNumbers". Matching any *upc* key is a guess that should
    be replaced with the real field(s) once known. Counters like "colorwayUpcCount"
    are ignored by the length check.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += [u for u in extract_upcs(value, _under_upc or "upc" in key.lower()) if u not in found]
    elif isinstance(node, list):
        for value in node:
            found += [u for u in extract_upcs(value, _under_upc) if u not in found]
    elif _under_upc and isinstance(node, (int, str)) and not isinstance(node, bool):
        s = str(node).strip()
        if s.isdigit() and len(s) == config.UPC_LENGTH:
            found.append(s)
    return found


async def send_to_als(client: httpx.AsyncClient, r: ItemResult) -> dict:
    return await request_json(
        client, "POST", f"{config.ALS_SERVICE_URL}/als/process",
        json={"upc": r.upc, "environment": r.environment, "item_id": r.item_id,
              "source_code": r.source_code,
              "resolved_via": "direct" if r.code_type == "upc" else "fcc"},
    )


async def get_als_status(client: httpx.AsyncClient, upc: str, env: str | None = None) -> dict:
    """Inventory status for a UPC, in one environment or (env=None) across all of them.

    Real mode: availability isn't per environment; it's scoped by the ALS_REAL_PARAMS
    (division, channel, fulfillment method, pickup location), and env is just echoed back.
    """
    if config.REAL:
        p = config.ALS_REAL_PARAMS
        data = await request_json(
            client, "GET", f"{config.ALS_REAL_URL}/v2/availability/divn/{p['divn']}/upc/{upc}",
            params={k: p[k] for k in ("availabilityType", "ffm", "country", "channel", "pickupLocation")},
        )
        entries = [n for item in data.get("items", []) for n in item.get("networkAvailability", [])]
        if not entries:
            return {"upc": upc, "environment": env, "available": False, "quantity": 0,
                    "status": "unknown", "reason": "ALS returned no availability entries"}
        available = any(e.get("available") for e in entries)
        first = entries[0]
        return {"upc": upc, "environment": env, "available": available,
                "quantity": max(e.get("maxQuantity", 0) for e in entries),
                "status": "in_stock" if available else "unavailable",
                "inventory_status_code": first.get("inventoryStatusCode"),
                "reason_code": first.get("reasonCode"), "reason": first.get("reasonDescription")}
    params = {"upc": upc}
    if env:
        params["environment"] = env
    return await request_json(client, "GET", f"{config.ALS_SERVICE_URL}/als/status", params=params)


# ---------------------------------------------------------------------------
# Per-item processing
# ---------------------------------------------------------------------------
async def process_item(client, fcc: FCCClient, sem: asyncio.Semaphore,
                       env: str, item: dict, dry_run: bool) -> ItemResult:
    code = str(item["code"]).strip()
    r = ItemResult(environment=env, item_id=item["item_id"], source_code=code, code_type=classify(code))

    async with sem:
        # Step 1: get a UPC
        if r.code_type == "upc":
            r.upc = code
        else:
            try:
                r.upc = await fcc.resolve(env, code)
            except PermanentError as exc:
                r.outcome = "fcc_not_found" if exc.status == 404 else "fcc_error"
                r.error = str(exc)
                return r
            except Exception as exc:  # noqa: BLE001
                r.outcome, r.error = "fcc_error", str(exc)
                return r

        # Step 2: send to ALS
        if dry_run:
            r.outcome = "dry_run_direct" if r.code_type == "upc" else "dry_run_via_fcc"
            return r
        try:
            resp = await send_to_als(client, r)
            r.als_id = resp.get("als_id")
            r.outcome = "sent_direct" if r.code_type == "upc" else "sent_via_fcc"
        except PermanentError as exc:
            r.outcome, r.error = "als_rejected", str(exc)
        except Exception as exc:  # noqa: BLE001
            r.outcome, r.error = "als_error", str(exc)
    return r


async def process_environment(client, fcc, sem, env: str, dry_run: bool) -> tuple[list[ItemResult], str | None]:
    try:
        items = await get_all_items(client, env)
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] could not read items: %s", env, exc)
        return [], str(exc)
    log.info("[%s] %d items", env, len(items))
    results = await asyncio.gather(*(process_item(client, fcc, sem, env, it, dry_run) for it in items))
    return list(results), None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run(envs: list[str] | None = None, dry_run: bool = False) -> dict:
    start = time.perf_counter()
    sem = asyncio.Semaphore(config.MAX_CONCURRENCY)

    async with httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT_S) as client:
        all_envs = await get_environments(client)
        targets = envs or all_envs
        unknown = [e for e in targets if e not in all_envs]
        if unknown:
            raise ValueError(f"Unknown environment(s): {unknown}. Available: {all_envs}")

        fcc = FCCClient(client)
        per_env = await asyncio.gather(*(process_environment(client, fcc, sem, e, dry_run) for e in targets))

    results: list[ItemResult] = []
    env_errors: dict[str, str] = {}
    by_env: dict[str, dict] = {}
    for env, (env_results, err) in zip(targets, per_env):
        results.extend(env_results)
        if err:
            env_errors[env] = err
        by_env[env] = dict(Counter(r.outcome for r in env_results))

    summary = {
        "environments": targets,
        "total_items": len(results),
        "by_code_type": dict(Counter(r.code_type for r in results)),
        "by_outcome": dict(Counter(r.outcome for r in results)),
        "by_environment": by_env,
        "env_errors": env_errors,
        "fcc_calls": fcc.calls,
        "dry_run": dry_run,
        "elapsed_s": round(time.perf_counter() - start, 3),
    }

    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(config.OUTPUT_DIR, "results.json"), "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    with open(os.path.join(config.OUTPUT_DIR, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def print_summary(s: dict) -> None:
    print("\n=== Pipeline summary ===")
    print(f"Environments: {', '.join(s['environments'])}")
    print(f"Items: {s['total_items']}   by code type: {s['by_code_type']}   FCC calls: {s['fcc_calls']}")
    print("Outcomes:")
    for k, v in sorted(s["by_outcome"].items()):
        print(f"  {k:<16} {v}")
    print("Per environment:")
    for env, counts in s["by_environment"].items():
        print(f"  {env:<16} {counts}")
    if s["env_errors"]:
        print(f"Environment errors: {s['env_errors']}")
    print(f"Elapsed: {s['elapsed_s']}s   Results: {config.OUTPUT_DIR}/results.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", action="append", help="environment to process (repeatable); default all")
    parser.add_argument("--dry-run", action="store_true", help="resolve UPCs but don't send to ALS")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        print_summary(asyncio.run(run(args.env, args.dry_run)))
    except ValueError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
