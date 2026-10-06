"""Keyless, offline test of the Teams bot, end to end except for Teams itself.

Starts the real bot endpoint (POST /api/messages, Microsoft Agents SDK) with a scripted
stand-in for Gemini, and a fake Teams "channel service" that records what the bot sends back.
Posts Teams-shaped activities and checks the replies. No Teams, Azure, API key or network.

Usage:  python test_teams_bot.py
"""
import asyncio
import logging
import sys
import warnings
from os import environ

import httpx
from aiohttp import web
from google.adk.agents import Agent
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import types

import teams_bot.app as bot   # sets UPC_BACKEND=real and loads .env files
from teams_bot.chat import ERROR_REPLY, AgentChat

warnings.filterwarnings("ignore")
logging.disable(logging.CRITICAL)
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)


class ScriptedLlm(BaseLlm):
    """Echoes the last user message and how many user turns it can see (proves history is kept)."""

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False):
        user_turns = [c for c in llm_request.contents if c.role == "user" and any(p.text for p in c.parts)]
        text = user_turns[-1].parts[-1].text
        if text == "boom":
            raise RuntimeError("secret-host.internal exploded")
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=f"echo:{text} (turn {len(user_turns)})")]))


async def start(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def main() -> None:
    # Fake Teams: records every activity the bot sends back.
    sent: list[dict] = []

    async def capture(request: web.Request) -> web.Response:
        sent.append(await request.json())
        return web.json_response({"id": "reply-1"})

    teams = web.Application()
    teams.router.add_post("/v3/conversations/{cid}/activities", capture)
    teams.router.add_post("/v3/conversations/{cid}/activities/{aid}", capture)
    teams_runner, teams_port = await start(teams)

    agent = Agent(name="scripted_triage", model=ScriptedLlm(model="scripted"), instruction="test", tools=[])
    manager, sdk_config = bot.build_connection_manager(allow_anonymous=True)
    bot_runner, bot_port = await start(bot.create_app(AgentChat(agent), manager, sdk_config))
    url = f"http://127.0.0.1:{bot_port}"

    def activity(text="", conv="c1", type="message", **extra) -> dict:
        return {"type": type, "id": "a1", "channelId": "msteams", "serviceUrl": f"http://127.0.0.1:{teams_port}/",
                "from": {"id": "user1", "name": "User"}, "recipient": {"id": "bot1", "name": "Bot"},
                "conversation": {"id": conv}, "text": text, **extra}

    async def say(client: httpx.AsyncClient, *args, **kwargs) -> list[str]:
        """Posts an activity; returns the text of the messages the bot sent back."""
        before = len(sent)
        resp = await client.post(f"{url}/api/messages", json=activity(*args, **kwargs), timeout=30)
        assert resp.status_code in (200, 201, 202), f"bot returned {resp.status_code}: {resp.text}"
        return [a.get("text", "") for a in sent[before:] if a.get("type") == "message"]

    async with httpx.AsyncClient() as client:
        print("\n=== Teams bot: messages ===")
        check("health endpoint", (await client.get(f"{url}/health")).json() == {"status": "ok"})
        r = await say(client, "hello")
        check("message gets the agent's reply", r == ["echo:hello (turn 1)"], str(r))
        r = await say(client, "again")
        check("same conversation keeps history", r == ["echo:again (turn 2)"], str(r))
        r = await say(client, "hi", conv="c2")
        check("conversations are isolated", r == ["echo:hi (turn 1)"], str(r))

        r = await say(client, "reset")
        check("reset acknowledged", len(r) == 1 and "fresh" in r[0], str(r))
        r = await say(client, "after reset")
        check("reset forgets history", r == ["echo:after reset (turn 1)"], str(r))

        mention = {"type": "mention", "text": "<at>Bot</at>", "mentioned": {"id": "bot1", "name": "Bot"}}
        r = await say(client, "<at>Bot</at> 28399242 in mcore-012", conv="c3", entities=[mention])
        check("@mention is stripped", r == ["echo:28399242 in mcore-012 (turn 1)"], str(r))
        check("blank message ignored", await say(client, "   ", conv="c4") == [])

        r = await say(client, "boom", conv="c5")
        check("agent failure -> friendly reply", r == [ERROR_REPLY], str(r))
        check("error details not leaked", "secret-host" not in " ".join(r))
        r = await say(client, "still alive", conv="c5")
        check("bot keeps working after a failure", len(r) == 1 and r[0].startswith("echo:still alive"), str(r))

        check("single newlines become paragraph breaks", bot.for_teams("a\nb\n\nc\n- d\n- e") == "a\n\nb\n\nc\n\n- d\n\n- e",
              repr(bot.for_teams("a\nb\n\nc")))

        print("\n=== Teams bot: conversation updates ===")
        r = await say(client, type="conversationUpdate", conv="c6", membersAdded=[{"id": "bot1", "name": "Bot"}])
        check("welcome when the bot is added", r == [bot.WELCOME], str(r)[:60])
        r = await say(client, type="conversationUpdate", conv="c7", membersAdded=[{"id": "someone-else"}])
        check("no welcome when someone else joins", r == [], str(r))

    print("\n=== Teams bot: authentication ===")
    creds = {"CLIENTID": "00000000-0000-0000-0000-000000000001", "CLIENTSECRET": "not-a-real-secret",
             "TENANTID": "00000000-0000-0000-0000-000000000002"}
    secured, secured_port = None, None
    try:
        from microsoft_agents.authentication.msal import MsalConnectionManager
        from microsoft_agents.hosting.core import AgentAuthConfiguration
        cfg = AgentAuthConfiguration(client_id=creds["CLIENTID"], client_secret=creds["CLIENTSECRET"],
                                     tenant_id=creds["TENANTID"])
        secured, secured_port = await start(bot.create_app(
            AgentChat(agent), MsalConnectionManager(connections_configurations={"SERVICE_CONNECTION": cfg})))
    except Exception as exc:  # noqa: BLE001
        print(f"  [SKIP] couldn't start a credentialed bot offline: {type(exc).__name__}: {str(exc)[:80]}")
    if secured:
        async with httpx.AsyncClient() as client:
            u = f"http://127.0.0.1:{secured_port}/api/messages"
            r = await client.post(u, json=activity("hi"))
            check("no token -> 401", r.status_code == 401, str(r.status_code))
            r = await client.post(u, json=activity("hi"), headers={"Authorization": "Bearer not.a.jwt"})
            check("bad token -> 401", r.status_code == 401, str(r.status_code))
            check("health stays open", (await client.get(f"http://127.0.0.1:{secured_port}/health")).status_code == 200)
        await secured.cleanup()

    has_creds = any(k.startswith("CONNECTIONS__SERVICE_CONNECTION") for k in environ)
    if not has_creds:
        try:
            bot.build_connection_manager(allow_anonymous=False)
            check("refuses to start without credentials", False)
        except SystemExit:
            check("refuses to start without credentials or opt-in", True)

    await bot_runner.cleanup()
    await teams_runner.cleanup()


asyncio.run(main())
print(f"\n{'All checks passed.' if not failures else f'{len(failures)} FAILED: {failures}'}")
sys.exit(1 if failures else 0)
