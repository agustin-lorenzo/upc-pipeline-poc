"""Pipeline functions exposed as ADK tools.

Each tool is a plain (async) Python function. ADK builds the tool schema the
model sees from the signature and the docstring, so the docstrings here are
written for the model: what the tool does, when to use it, and what comes back.

Every tool returns a dict with a "status" key:
  "success"    -> the call worked; the rest of the dict is the payload
  "not_found"  -> the thing asked about doesn't exist (e.g. FCC has no UPC)
  "error"      -> something failed; see "error_message"

Read-only tools: list_environments, get_environment_items, classify_code,
                 resolve_upc, get_run_results, check_code_status, check_environment_status
Tools that write to ALS (need confirmation unless dry_run=True):
                 send_upc_to_als, process_item, run_pipeline
"""
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict

import httpx

# Make the project root importable no matter where `adk web` / `adk run` is launched from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import pipeline  # noqa: E402

MAX_ITEMS_PER_CALL = 100
MAX_RESULTS_PER_CALL = 50


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=config.REQUEST_TIMEOUT_S)


def _error(exc: Exception) -> dict:
    return {"status": "error", "error_message": str(exc)}


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------
async def list_environments() -> dict:
    """Lists every live environment that items can be pulled from.

    Returns:
        dict: {"status": "success", "environments": [environment names]}
    """
    try:
        async with _client() as client:
            return {"status": "success", "environments": await pipeline.get_environments(client)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def get_environment_items(environment: str, offset: int = 0, limit: int = 50) -> dict:
    """Gets one page of items from an environment, with each code already classified.

    Each item's code is either a UPC (goes straight to ALS) or an
    environment-specific local code (must be resolved through FCC first).

    Args:
        environment: Environment name, as returned by list_environments.
        offset: Index of the first item to return. Use next_offset from the previous page.
        limit: Items per page (max 100).

    Returns:
        dict: {"status": "success", "environment", "items": [{"item_id", "code", "code_type"}],
               "counts": {"upc": n, "local": n}, "total": int, "next_offset": int or null}
    """
    limit = max(1, min(limit, MAX_ITEMS_PER_CALL))
    try:
        async with _client() as client:
            page = await pipeline.request_json(
                client, "GET", f"{config.ENV_SERVICE_URL}/environments/{environment}/items",
                params={"offset": offset, "limit": limit},
            )
    except pipeline.PermanentError as exc:
        if exc.status == 404:
            return {"status": "not_found", "error_message": f"Unknown environment '{environment}'"}
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    items = [{**it, "code_type": pipeline.classify(str(it["code"]))} for it in page["items"]]
    return {
        "status": "success",
        "environment": environment,
        "items": items,
        "counts": dict(Counter(it["code_type"] for it in items)),
        "total": page["total"],
        "next_offset": page.get("next_offset"),
    }


def classify_code(code: str) -> dict:
    """Says whether a code is a UPC or an environment-specific local code.

    Args:
        code: The code as it came from the environment.

    Returns:
        dict: {"status": "success", "code", "code_type": "upc" | "local", "needs_fcc": bool}
    """
    code_type = pipeline.classify(code)
    return {"status": "success", "code": code.strip(), "code_type": code_type,
            "needs_fcc": code_type == "local", "upc_length": config.UPC_LENGTH}


async def resolve_upc(environment: str, code: str) -> dict:
    """Looks up the UPC for an environment-specific local code using FCC.

    Local codes are only unique within an environment, so the environment is required.
    Don't call this for codes that are already UPCs.

    Args:
        environment: The environment the code came from.
        code: The local (non-UPC) code.

    Returns:
        dict: {"status": "success", "environment", "code", "upc"}, or
              {"status": "not_found", ...} if FCC has no UPC for that code in that environment.
    """
    try:
        async with _client() as client:
            upc = await pipeline.FCCClient(client).resolve(environment, code.strip())
        return {"status": "success", "environment": environment, "code": code.strip(), "upc": upc}
    except pipeline.PermanentError as exc:
        if exc.status == 404:
            return {"status": "not_found", "environment": environment, "code": code,
                    "error_message": "FCC has no UPC for this code in this environment"}
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def check_code_status(code: str, environment: str = "") -> dict:
    """Checks whether a UPC or local code is available in ALS inventory. Read-only.

    A UPC is checked directly. A local code is first resolved to a UPC through FCC, which
    needs the environment because local codes are only unique within one. For a UPC, the
    environment is optional: leave it empty to see availability in every environment.

    Args:
        code: A UPC or an environment-specific local code.
        environment: Environment name. Required for local codes; optional for UPCs.

    Returns:
        dict: {"status": "success", "code", "code_type", "upc", "environment", "available": bool,
               "quantity", "inventory_status": "in_stock" | "out_of_stock" | "not_in_inventory"}.
              For a UPC with no environment: "available" (anywhere) and "by_environment" instead.
              "not_found" if FCC can't resolve the local code or the environment is unknown.
              "error" if a local code is given without an environment.
    """
    code, environment = code.strip(), environment.strip()
    code_type = pipeline.classify(code)
    try:
        async with _client() as client:
            if code_type == "local":
                if not environment:
                    return {"status": "error", "error_message":
                            f"'{code}' is a local code, so I need the environment to look it up."}
                upc = await pipeline.FCCClient(client).resolve(environment, code)
            else:
                upc = code
            als = await pipeline.get_als_status(client, upc, environment or None)
    except pipeline.PermanentError as exc:
        if exc.status == 404:
            return {"status": "not_found", "code": code, "environment": environment, "error_message": str(exc)}
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    if "status" in als:  # single-environment answer; rename so it can't clash with our own "status"
        als["inventory_status"] = als.pop("status")
    if "by_environment" in als:
        for s in als["by_environment"].values():
            s["inventory_status"] = s.pop("status")
    return {**als, "status": "success", "code": code, "code_type": code_type, "upc": upc}


async def check_environment_status(environment: str, max_listed: int = 15) -> dict:
    """Checks ALS inventory availability for every item in an environment. Read-only.

    Pulls all the environment's items, resolves local codes through FCC, and asks ALS about
    each UPC. Items whose local code FCC can't resolve are counted separately.

    Args:
        environment: Environment name, as returned by list_environments.
        max_listed: Max unavailable items to list individually (max 50); the counts cover all items.

    Returns:
        dict: {"status": "success", "environment", "total_items", "counts": {"in_stock": n,
               "out_of_stock": n, "not_in_inventory": n, "unresolved": n},
               "unavailable": [{"item_id", "code", "upc", "inventory_status"}], "unavailable_total": int}
              or "not_found" for an unknown environment.
    """
    max_listed = max(1, min(max_listed, MAX_RESULTS_PER_CALL))
    try:
        async with _client() as client:
            items = await pipeline.get_all_items(client, environment)
            fcc, sem = pipeline.FCCClient(client), asyncio.Semaphore(config.MAX_CONCURRENCY)

            async def one(item: dict) -> dict:
                code = str(item["code"]).strip()
                row = {"item_id": item["item_id"], "code": code, "upc": None}
                async with sem:
                    try:
                        row["upc"] = code if pipeline.classify(code) == "upc" else await fcc.resolve(environment, code)
                        row["inventory_status"] = (await pipeline.get_als_status(client, row["upc"], environment))["status"]
                    except pipeline.PermanentError as exc:
                        row["inventory_status"] = "unresolved" if exc.status == 404 else "error"
                        row["error"] = str(exc)
                return row

            rows = await asyncio.gather(*(one(it) for it in items))
    except pipeline.PermanentError as exc:
        if exc.status == 404:
            return {"status": "not_found", "error_message": f"Unknown environment '{environment}'"}
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    counts = {k: 0 for k in ("in_stock", "out_of_stock", "not_in_inventory", "unresolved")}
    counts.update(Counter(r["inventory_status"] for r in rows))
    unavailable = [r for r in rows if r["inventory_status"] != "in_stock"]
    return {"status": "success", "environment": environment, "total_items": len(rows), "counts": counts,
            "unavailable": unavailable[:max_listed], "unavailable_total": len(unavailable)}


def get_run_results(outcome: str = "", environment: str = "", limit: int = 20) -> dict:
    """Reads per-item results from the most recent pipeline run (from run_pipeline).

    Use this to explain failures, e.g. outcome="fcc_not_found".

    Args:
        outcome: Optional filter. One of: sent_direct, sent_via_fcc, fcc_not_found,
            fcc_error, als_rejected, als_error, dry_run_direct, dry_run_via_fcc.
            Empty string means no filter.
        environment: Optional environment filter. Empty string means all.
        limit: Max rows to return (max 50).

    Returns:
        dict: {"status": "success", "matched": int, "results": [rows]}
    """
    path = os.path.join(config.OUTPUT_DIR, "results.json")
    if not os.path.exists(path):
        return {"status": "not_found", "error_message": "No pipeline run found yet. Call run_pipeline first."}
    with open(path) as f:
        rows = json.load(f)
    if outcome:
        rows = [r for r in rows if r["outcome"] == outcome]
    if environment:
        rows = [r for r in rows if r["environment"] == environment]
    limit = max(1, min(limit, MAX_RESULTS_PER_CALL))
    return {"status": "success", "matched": len(rows), "results": rows[:limit]}


# ---------------------------------------------------------------------------
# Tools that write to ALS
# ---------------------------------------------------------------------------
async def send_upc_to_als(upc: str, environment: str, item_id: str, source_code: str) -> dict:
    """Sends one UPC to ALS for processing. This writes to ALS.

    Only use this for a UPC you already have (from the environment or from resolve_upc),
    e.g. to retry a single item. To handle an item from scratch, use process_item.

    Args:
        upc: The UPC to send.
        environment: Environment the item came from.
        item_id: The item's ID in that environment.
        source_code: The code the environment originally returned (the UPC itself, or the local code).

    Returns:
        dict: {"status": "success", "als_id", "upc"}, or status "error" if ALS rejected it.
    """
    r = pipeline.ItemResult(environment=environment, item_id=item_id, source_code=source_code,
                            code_type=pipeline.classify(source_code), upc=upc.strip())
    try:
        async with _client() as client:
            resp = await pipeline.send_to_als(client, r)
        return {"status": "success", "als_id": resp.get("als_id"), "upc": r.upc}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def process_item(environment: str, item_id: str, code: str, dry_run: bool = True) -> dict:
    """Runs one item through the full flow: classify, resolve via FCC if needed, send to ALS.

    Args:
        environment: Environment the item came from.
        item_id: The item's ID.
        code: The code the environment returned for the item.
        dry_run: If true (default), resolve the UPC but don't send it to ALS.
            Set false to actually send; the user will be asked to confirm.

    Returns:
        dict: {"status": "success" | "error", "result": {environment, item_id, source_code,
               code_type, upc, outcome, als_id, error}}
    """
    try:
        async with _client() as client:
            r = await pipeline.process_item(
                client, pipeline.FCCClient(client), asyncio.Semaphore(1),
                environment, {"item_id": item_id, "code": code}, dry_run,
            )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    ok = r.outcome in ("sent_direct", "sent_via_fcc", "dry_run_direct", "dry_run_via_fcc")
    return {"status": "success" if ok else "error", "result": asdict(r)}


async def run_pipeline(environments: list[str] | None = None, dry_run: bool = True) -> dict:
    """Runs the whole pipeline over every item in one or more environments.

    Pulls all items, resolves local codes through FCC, and sends UPCs to ALS.
    Per-item results are saved and can be read with get_run_results.

    Args:
        environments: Environments to process. Omit to process all of them.
        dry_run: If true (default), resolve everything but send nothing to ALS.
            Set false to actually send; the user will be asked to confirm.

    Returns:
        dict: {"status": "success", "summary": {total_items, by_code_type, by_outcome,
               by_environment, env_errors, fcc_calls, dry_run, elapsed_s}}
    """
    try:
        summary = await pipeline.run(environments or None, dry_run)
        return {"status": "success", "summary": summary}
    except ValueError as exc:
        return {"status": "not_found", "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def needs_confirmation(dry_run: bool = True, **_: object) -> bool:
    """Confirmation policy for tools with a dry_run flag: only real sends need approval."""
    return not dry_run
