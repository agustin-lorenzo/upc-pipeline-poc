"""Keyless smoke test for the ADK agent.

Starts the mock services, then:
  1. calls every tool directly and checks the results
  2. runs the real agent + tools through ADK's Runner with a scripted fake LLM
     standing in for Gemini, covering the confirmation flow for ALS sends
     (approve and reject).

No API key or network needed. Usage:  python test_agent.py
"""
import asyncio
import json
import logging
import sys
import warnings
from collections import Counter

import httpx
from google.adk.agents import Agent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

import config
import run_all
from upc_agent import tools
from upc_agent.agent import root_agent

warnings.filterwarnings("ignore")
logging.disable(logging.WARNING)  # hides ADK's "missing token usage" notes for the fake model
CONFIRM = "adk_request_confirmation"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


# ---------------------------------------------------------------------------
# 1. Direct tool calls
# ---------------------------------------------------------------------------
async def test_tools() -> None:
    print("\n=== Tools, called directly ===")
    envs = await tools.list_environments()
    check("list_environments", envs["status"] == "success" and len(envs["environments"]) == 4, str(envs.get("environments")))
    env = envs["environments"][0]

    page = await tools.get_environment_items(env, limit=100)
    check("get_environment_items", page["status"] == "success" and page["total"] == len(page["items"]), str(page["counts"]))
    check("get_environment_items unknown env", (await tools.get_environment_items("nope"))["status"] == "not_found")

    upc_item = next(i for i in page["items"] if i["code_type"] == "upc")
    local_item = next(i for i in page["items"] if i["code_type"] == "local")
    check("classify_code", tools.classify_code(upc_item["code"])["code_type"] == "upc"
          and tools.classify_code(local_item["code"])["needs_fcc"])

    res = await tools.resolve_upc(env, local_item["code"])
    check("resolve_upc", res["status"] == "success" and len(res["upc"]) == config.UPC_LENGTH, res.get("upc", ""))
    check("resolve_upc not found", (await tools.resolve_upc(env, "000000"))["status"] == "not_found")

    dry = await tools.process_item(env, local_item["item_id"], local_item["code"], dry_run=True)
    check("process_item dry run", dry["result"]["outcome"] == "dry_run_via_fcc")

    sent = await tools.send_upc_to_als(res["upc"], env, local_item["item_id"], local_item["code"])
    check("send_upc_to_als", sent["status"] == "success" and sent["als_id"])
    bad = await tools.send_upc_to_als("123", env, "x", "123")
    check("send_upc_to_als rejects bad UPC", bad["status"] == "error", bad.get("error_message", "")[:60])

    run = await tools.run_pipeline([env], dry_run=True)
    check("run_pipeline dry run", run["status"] == "success" and run["summary"]["dry_run"], str(run["summary"]["by_outcome"]))
    check("run_pipeline unknown env", (await tools.run_pipeline(["nope"]))["status"] == "not_found")

    nf = tools.get_run_results(outcome="fcc_not_found")
    check("get_run_results filter", nf["status"] == "success" and all(r["outcome"] == "fcc_not_found" for r in nf["results"]),
          f"{nf['matched']} fcc_not_found")

    # ALS inventory status, checked against the ground truth in dummy_data.json
    with open(config.DATA_FILE) as f:
        data = json.load(f)
    inv, maps = data["inventory"], data["fcc_mappings"]

    def expected_status(e, upc):
        q = inv[e].get(upc)
        return "not_in_inventory" if q is None else "in_stock" if q > 0 else "out_of_stock"

    for want in ("in_stock", "out_of_stock", "not_in_inventory"):
        upc = next(u for u in (i["code"] for i in page["items"] if i["code_type"] == "upc")
                   if expected_status(env, u) == want)
        r = await tools.check_code_status(upc, env)
        check(f"check_code_status UPC {want}", r["status"] == "success" and r["inventory_status"] == want
              and r["available"] == (want == "in_stock"), f"{upc} qty={r['quantity']}")

    mapped = next(i for i in page["items"] if i["code"] in maps[env])
    r = await tools.check_code_status(mapped["code"], env)
    check("check_code_status local code via FCC", r["upc"] == maps[env][mapped["code"]]
          and r["inventory_status"] == expected_status(env, r["upc"]), f"{mapped['code']} -> {r['upc']}")
    r = await tools.check_code_status(mapped["code"])
    check("check_code_status local code needs environment", r["status"] == "error")
    r = await tools.check_code_status("000000", env)
    check("check_code_status unresolvable local code", r["status"] == "not_found")
    r = await tools.check_code_status(upc_item["code"])
    check("check_code_status UPC across environments", r["status"] == "success" and set(r["by_environment"]) == set(inv)
          and r["available"] == any(expected_status(e, upc_item["code"]) == "in_stock" for e in inv))
    check("check_code_status unknown environment", (await tools.check_code_status(upc_item["code"], "nope"))["status"] == "not_found")

    for e in inv:
        r = await tools.check_environment_status(e)
        unresolved = sum(1 for i in data["environments"][e] if len(i["code"]) != config.UPC_LENGTH and i["code"] not in maps[e])
        want = Counter(expected_status(e, i["code"] if len(i["code"]) == config.UPC_LENGTH else maps[e][i["code"]])
                       for i in data["environments"][e]
                       if len(i["code"]) == config.UPC_LENGTH or i["code"] in maps[e])
        ok = (r["status"] == "success" and r["total_items"] == len(data["environments"][e])
              and all(r["counts"][k] == want[k] for k in want) and r["counts"]["unresolved"] == unresolved)
        check(f"check_environment_status {e}", ok, str(r.get("counts")))
    check("check_environment_status unknown env", (await tools.check_environment_status("nope"))["status"] == "not_found")

    check("confirmation policy", tools.needs_confirmation(dry_run=False) and not tools.needs_confirmation(dry_run=True))


# ---------------------------------------------------------------------------
# 2. Agent loop with a scripted LLM
# ---------------------------------------------------------------------------
class ScriptedLlm(BaseLlm):
    """Stands in for Gemini. Maps user messages to tool calls and summarises tool results."""

    script: dict = {}

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        last = llm_request.contents[-1].parts[-1]
        if last.function_response:
            resp = last.function_response.response or {}
            text = f"[{last.function_response.name}] status={resp.get('status', resp.get('error'))}"
            if "summary" in resp:
                text += f" outcomes={resp['summary']['by_outcome']}"
            yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
            return
        name, args = self.script[last.text]
        yield LlmResponse(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name=name, args=args))]))


async def send(runner, session_id, content):
    events = [e async for e in runner.run_async(user_id="u", session_id=session_id, new_message=content)]
    calls = [fc for e in events for fc in (e.get_function_calls() or [])]
    texts = [p.text for e in events if e.content for p in e.content.parts if p.text]
    return calls, texts


def user(text):
    return types.Content(role="user", parts=[types.Part(text=text)])


def confirmation(call_id, confirmed):
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=call_id, name=CONFIRM, response={"confirmed": confirmed}))])


async def test_agent_loop() -> None:
    print("\n=== Agent loop (real agent + tools, scripted LLM) ===")
    env = "dc-duluth"
    llm = ScriptedLlm(model="scripted", script={
        "what environments are there?": ("list_environments", {}),
        "dry run dc-duluth": ("run_pipeline", {"environments": [env], "dry_run": True}),
        "send dc-duluth": ("run_pipeline", {"environments": [env], "dry_run": False}),
        "what failed?": ("get_run_results", {"outcome": "fcc_not_found"}),
        "status of dc-duluth": ("check_environment_status", {"environment": env}),
    })
    agent = Agent(name=root_agent.name, model=llm, instruction=root_agent.instruction, tools=root_agent.tools)
    sessions = InMemorySessionService()
    runner = Runner(agent=agent, app_name="upc", session_service=sessions)
    s = await sessions.create_session(app_name="upc", user_id="u")
    als = f"{config.ALS_SERVICE_URL}/als"
    httpx.post(f"{als}/reset")

    calls, texts = await send(runner, s.id, user("what environments are there?"))
    check("agent calls list_environments", [c.name for c in calls] == ["list_environments"], texts[-1])

    calls, texts = await send(runner, s.id, user("dry run dc-duluth"))
    check("dry run needs no confirmation", CONFIRM not in [c.name for c in calls], texts[-1])
    check("dry run sends nothing to ALS", len(httpx.get(f"{als}/received").json()) == 0)

    # Real send -> paused for confirmation -> reject
    calls, _ = await send(runner, s.id, user("send dc-duluth"))
    req = next((c for c in calls if c.name == CONFIRM), None)
    check("real send pauses for confirmation", req is not None)
    check("nothing sent before confirmation", len(httpx.get(f"{als}/received").json()) == 0)
    _, texts = await send(runner, s.id, confirmation(req.id, False))
    check("rejected send writes nothing", len(httpx.get(f"{als}/received").json()) == 0, texts[-1] if texts else "")

    # Real send -> approve
    calls, _ = await send(runner, s.id, user("send dc-duluth"))
    req = next(c for c in calls if c.name == CONFIRM)
    _, texts = await send(runner, s.id, confirmation(req.id, True))
    received = httpx.get(f"{als}/received").json()
    check("approved send reaches ALS", len(received) > 0 and all(r["environment"] == env for r in received),
          f"ALS got {len(received)} items; {texts[-1] if texts else ''}")

    calls, texts = await send(runner, s.id, user("what failed?"))
    check("agent reads run results", [c.name for c in calls] == ["get_run_results"], texts[-1])

    calls, texts = await send(runner, s.id, user("status of dc-duluth"))
    check("status lookup needs no confirmation", [c.name for c in calls] == ["check_environment_status"], texts[-1])


def main() -> None:
    import subprocess
    subprocess.run([sys.executable, "generate_dummy_data.py"], cwd=run_all.ROOT, check=True, capture_output=True)
    procs = run_all.start_services()
    try:
        run_all.wait_healthy()
        asyncio.run(test_tools())
        asyncio.run(test_agent_loop())
    finally:
        for p in procs:
            p.terminate()
            p.wait(timeout=5)
    print(f"\n{'All checks passed.' if not failures else f'{len(failures)} FAILED: {failures}'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
