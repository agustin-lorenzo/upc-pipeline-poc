"""Runs an ADK agent for chat messages, with one conversation = one agent session.

Deliberately knows nothing about Teams or the Microsoft Agents SDK, so it can be tested
on its own and reused behind any other chat front end.

Sessions live in memory: a restart forgets them, and they aren't shared between server
instances. Fine for a prototype; use a persistent ADK session service before running
more than one instance.
"""
import asyncio
import logging

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

log = logging.getLogger("teams_bot")

ERROR_REPLY = "Sorry, something went wrong while looking that up. Please try again in a moment."
EMPTY_REPLY = "I didn't get an answer back. Please try again."


class AgentChat:
    def __init__(self, agent, app_name: str = "environment_triage"):
        self._app_name = app_name
        self._sessions = InMemorySessionService()
        self._runner = Runner(agent=agent, app_name=app_name, session_service=self._sessions)
        self._locks: dict[str, asyncio.Lock] = {}

    async def reply(self, conversation_id: str, text: str) -> str:
        """The agent's answer to one message. Never raises; failures become a short apology."""
        # One message at a time per conversation, so quick follow-ups don't interleave.
        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            try:
                return await self._run(conversation_id, text)
            except Exception:  # noqa: BLE001
                # Details go to the log, not the chat: they can contain hostnames and keys' error text.
                log.exception("Agent failed for conversation %s", conversation_id)
                return ERROR_REPLY

    async def reset(self, conversation_id: str) -> None:
        """Forgets the conversation so the next message starts fresh."""
        await self._sessions.delete_session(
            app_name=self._app_name, user_id=conversation_id, session_id=conversation_id)

    async def _run(self, conversation_id: str, text: str) -> str:
        ids = dict(app_name=self._app_name, user_id=conversation_id, session_id=conversation_id)
        if not await self._sessions.get_session(**ids):
            await self._sessions.create_session(**ids)
        message = types.Content(role="user", parts=[types.Part(text=text)])
        answer: list[str] = []
        async for event in self._runner.run_async(
                user_id=conversation_id, session_id=conversation_id, new_message=message):
            if event.is_final_response() and event.content and event.content.parts:
                answer += [p.text for p in event.content.parts if p.text]
        return "".join(answer).strip() or EMPTY_REPLY
