"""Microsoft Teams front end for the Environment Triage Agent.

Teams -> Azure Bot registration -> POST /api/messages (this app) -> ADK agent -> FCC / ALS.
Built on the Microsoft 365 Agents SDK for Python (aiohttp hosting).

Run:   python -m teams_bot.app          (listens on http://localhost:3978)
Local test without Teams, Azure or an API key:   python test_teams_bot.py
Try it in a Teams-like window: Microsoft 365 Agents Playground, pointed at /api/messages.

Auth: with bot credentials set (CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID/
CLIENTSECRET/TENANTID), every request must carry a valid Microsoft-issued JWT. With none set,
the app refuses to start unless TEAMS_BOT_ALLOW_ANONYMOUS=true, which is for local testing
only and makes the app listen on localhost.

This bot is read-only, so it always runs the agent in real mode (UPC_BACKEND=real). The mock
pipeline agent needs approve/reject prompts for ALS writes, which Teams doesn't give us here.
"""
import logging
import os
import sys
from os import environ
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "upc_agent" / ".env")      # Gemini key + model, shared with `adk web`
load_dotenv(ROOT / "teams_bot" / ".env")      # bot credentials
if environ.setdefault("UPC_BACKEND", "real") != "real":
    raise SystemExit("The Teams bot only runs against the real, read-only endpoints (UPC_BACKEND=real).")

from aiohttp import web  # noqa: E402
from microsoft_agents.activity import load_configuration_from_env  # noqa: E402
from microsoft_agents.authentication.msal import MsalConnectionManager  # noqa: E402
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_decorator, start_agent_process  # noqa: E402
from microsoft_agents.hosting.core import (  # noqa: E402
    AgentApplication, AgentAuthConfiguration, MemoryStorage, TurnContext, TurnState,
)

from teams_bot.chat import AgentChat  # noqa: E402

log = logging.getLogger("teams_bot")

WELCOME = (
    "Hi, I'm the Environment Triage Agent. I check item availability.\n\n"
    "- Send a **product ID and environment** (e.g. `28399242 in mcore-012`) and I'll look up its UPC in FCC, then check ALS.\n"
    "- Or send a **UPC** and I'll check ALS directly.\n\n"
    "Type `reset` to start over."
)
RESET_WORDS = {"reset", "/reset", "start over"}


def build_connection_manager(allow_anonymous: bool) -> tuple[MsalConnectionManager, dict]:
    """Bot credentials from the environment, or an explicit anonymous config for local testing."""
    sdk_config = load_configuration_from_env(environ)
    if sdk_config.get("CONNECTIONS", {}).get("SERVICE_CONNECTION"):
        return MsalConnectionManager(**sdk_config), sdk_config
    if not allow_anonymous:
        raise SystemExit(
            "No bot credentials found. Set CONNECTIONS__SERVICE_CONNECTION__SETTINGS__CLIENTID, CLIENTSECRET "
            "and TENANTID (see teams_bot/.env.example), or set TEAMS_BOT_ALLOW_ANONYMOUS=true for local testing.")
    log.warning("Running with authentication OFF (TEAMS_BOT_ALLOW_ANONYMOUS). Local testing only.")
    manager = MsalConnectionManager(
        connections_configurations={"SERVICE_CONNECTION": AgentAuthConfiguration(anonymous_allowed=True)})
    return manager, sdk_config


def create_app(chat: AgentChat, connection_manager: MsalConnectionManager, sdk_config: dict | None = None) -> web.Application:
    adapter = CloudAdapter(connection_manager=connection_manager)
    agent_app = AgentApplication[TurnState](
        storage=MemoryStorage(), connection_manager=connection_manager, **(sdk_config or {}))

    @agent_app.conversation_update("membersAdded")
    async def on_members_added(context: TurnContext, _state: TurnState):
        bot_id = context.activity.recipient.id
        if any(m.id == bot_id for m in context.activity.members_added or []):   # the bot itself was added
            await context.send_activity(WELCOME)

    @agent_app.activity("message")
    async def on_message(context: TurnContext, _state: TurnState):
        text = (context.activity.text or "").strip()
        if not text:
            return
        conversation_id = context.activity.conversation.id
        if text.lower() in RESET_WORDS:
            await chat.reset(conversation_id)
            await context.send_activity("Okay, starting fresh.")
            return
        await context.send_activity(await chat.reply(conversation_id, text))

    @agent_app.error
    async def on_error(context: TurnContext, error: Exception):
        log.exception("Unhandled error in turn: %s", error)
        await context.send_activity("Sorry, something went wrong.")

    @jwt_authorization_decorator
    async def messages(request: web.Request) -> web.Response:
        return await start_agent_process(request, agent_app, adapter)

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    app = web.Application()
    app["agent_configuration"] = connection_manager.get_default_connection_configuration()
    app.router.add_post("/api/messages", messages)
    app.router.add_get("/health", health)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    from upc_agent.agent import root_agent   # after .env + UPC_BACKEND are set; agent reads them at import

    anonymous = environ.get("TEAMS_BOT_ALLOW_ANONYMOUS", "").lower() in ("1", "true", "yes")
    manager, sdk_config = build_connection_manager(anonymous)
    app = create_app(AgentChat(root_agent), manager, sdk_config)
    host = environ.get("HOST", "localhost" if manager.get_default_connection_configuration().ANONYMOUS_ALLOWED else "0.0.0.0")
    port = int(environ.get("PORT", "3978"))
    log.info("Environment Triage Agent listening on http://%s:%d/api/messages", host, port)
    web.run_app(app, host=host, port=port, print=None)


if __name__ == "__main__":
    main()
