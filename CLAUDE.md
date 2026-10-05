# UPC pipeline POC — context for Claude Code

## What this is
Proof of concept for Macy's: pull item codes from live environments, resolve non-UPC
codes to UPCs through FCC, and send every UPC to ALS. Everything runs locally against
mock services with dummy data. Real endpoint paths, payloads and auth are still unknown.

```
Environment -> code -> UPC_LENGTH digits? -- yes --------------------------> ALS
                                          -- no  -> FCC(environment, code) -> ALS
```

## Layout
- `config.py`: URLs, UPC_LENGTH, concurrency, retries. All values can be overridden with env vars.
- `generate_dummy_data.py` -> `data/dummy_data.json` (environments, FCC mappings, expected counts).
  Some local codes are deliberately left out of FCC's mapping. One local code is reused across
  environments with different UPCs, to prove lookups are scoped by environment.
- `mock_services/`: FastAPI apps for environment (:8001, paginated), FCC (:8002), ALS (:8003).
  ALS also serves `GET /als/status?upc=&environment=` (inventory: in_stock / out_of_stock /
  not_in_inventory), backed by `inventory` in dummy_data.json (per environment, generated with a
  separate RNG so items/mappings are unchanged).
- `pipeline.py`: the real logic, async httpx. Retries only on 5xx/network errors; 4xx raises
  `PermanentError`. FCC lookups are cached per (environment, code). Every item ends in exactly
  one outcome: sent_direct, sent_via_fcc, fcc_not_found, fcc_error, als_rejected, als_error
  (or dry_run_*). Writes `output/results.json` and `output/summary.json`.
  `run()` raises ValueError for unknown environments; don't change that back to SystemExit,
  because the ADK agent calls it.
- `verify.py`: checks a run against the ground truth in dummy_data.json.
- `run_all.py`: one-command demo. `--services-only` starts just the mocks (needed for the agent).
- `upc_agent/`: Google ADK agent. `tools.py` wraps pipeline functions as 10 tools (incl. read-only `check_code_status` and
  `check_environment_status`, which power the "type a code or environment, get ALS status" chat) that return
  `{"status": success|not_found|error, ...}`. `agent.py` defines `root_agent`
  (model from UPC_AGENT_MODEL, default gemini-3.5-flash).
  Tools that write to ALS use `FunctionTool(require_confirmation=...)`: send_upc_to_als always
  confirms, while process_item and run_pipeline confirm only when dry_run=False. dry_run
  defaults to True.
- `test_agent.py`: keyless smoke test. Calls the tools directly, then runs the real agent
  through ADK's Runner with a scripted fake LLM, covering both approve and reject for
  confirmations.

## Real endpoints (`UPC_BACKEND=real`)
Read-only real FCC (`.../api/catalog/v2/products/{productId}`, host contains the environment
name, e.g. mcore-012) and ALS (`/v2/availability/divn/12/upc/{upc}?...`) calls are wired in,
selected by `UPC_BACKEND=real` (`start.ps1 -Real`). UPC_LENGTH defaults to 12 in real mode.
In real mode `agent.py` builds the read-only "Environment Triage Agent" (tools: `check_availability`
with `upc` OR `product_id`+`environment`, and `resolve_upc`); everything else returns "not supported".
~23 environments exist; any name matching `ENV_NAME_PATTERN` (`^mcore-\d{3}$`) is accepted, no list.
FCC returns `200 {"product": {}}` for unknown products. Guesses, not facts: the
local code is the FCC product ID, UPCs are found by `pipeline.extract_upcs` (any key with "upc"),
and ALS params (divn 12, ffm STH, MCOM, 840, pickup 858) are copied from one example. `python
test_real_endpoints.py` checks the live endpoints (no auth was needed from the user's machine).

## Teams bot (`teams_bot/`)
Goal: the triage agent reachable from Macy's internal Microsoft Teams. `chat.py` (`AgentChat`: ADK
Runner + one in-memory session per conversation, SDK-independent) and `app.py` (aiohttp +
Microsoft 365 Agents SDK: `POST /api/messages`, JWT auth, welcome, `reset`). Always real mode,
read-only (no confirmation flow in Teams). `python test_teams_bot.py` is keyless/offline and drives
the real endpoint with a scripted LLM and a fake Teams channel service. Anonymous mode needs
`TEAMS_BOT_ALLOW_ANONYMOUS=true` (local only). Not yet done: running in real Teams or the Agents
Playground, hosting, and Azure Bot/Entra registration. The user doesn't yet know what Azure access,
hosting, or approved LLMs exist at Macy's; FCC/ALS hosts only resolved from the user's network.

## Commands
```bash
python run_all.py                  # pipeline end to end + verification (Python 3.9+)
python test_agent.py               # agent smoke test, no API key (Python 3.10+)
python run_all.py --services-only  # then `adk web` from the project root
```

## Constraints and decisions
- The user's Mac system Python is 3.9. Pipeline/mock files use `from __future__ import annotations`
  so `X | None` hints work on 3.9. google-adk requires Python 3.10+, so the agent needs a newer Python.
- UPC_LENGTH is 10 because that's what the user described. Real UPC-A is 12 digits, so confirm
  against the real data.
- Codes are classified by length because the environment doesn't label the code type. If the real
  endpoint does label it, use that label instead.
- To go live: set the three URLs in config and adapt `get_environments`, `get_all_items`,
  `FCCClient._fetch`, `send_to_als` and `get_als_status` in pipeline.py to the real endpoints. Nothing else should
  need to change.

## Open items
- Real endpoint specs and auth for the environment service, FCC and ALS.
- Whether ALS should be sent one UPC per item or a deduplicated set of UPCs.
- Not yet tested with a real Gemini model (only with the scripted LLM).
