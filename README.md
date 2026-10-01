# UPC Pipeline POC

Pulls item codes from live environments, resolves the ones that aren't UPCs through FCC, and sends every UPC to ALS. Ships with mock versions of all three services and dummy data so the whole thing runs locally.

```
Environment ──code──► is it a UPC (10 digits)?
                         ├─ yes ─────────────────────────► ALS
                         └─ no  ─► FCC(environment, code) ─► UPC ─► ALS
```

## Quick start

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python run_all.py
```

This generates dummy data, starts the three mock services on ports 8001–8003, runs the pipeline, verifies the results against ground truth, and shuts everything down.

## Files

| File | What it does |
|---|---|
| `config.py` | URLs, UPC length, concurrency, retries. All values can be overridden with env vars. |
| `generate_dummy_data.py` | Builds `data/dummy_data.json`: 4 environments, a mix of UPCs and 6-digit local codes, the FCC mapping table, and expected counts. Some local codes are left unmapped on purpose. |
| `mock_services/environment_service.py` | `GET /environments`, `GET /environments/{env}/items?offset=&limit=` (paginated) |
| `mock_services/fcc_service.py` | `GET /fcc/upc?environment=&code=` → `{upc}` or 404 |
| `mock_services/als_service.py` | `POST /als/process`, `GET /als/status?upc=&environment=` (inventory), `GET /als/received`, `POST /als/reset` |
| `pipeline.py` | The actual pipeline. This is the part you keep. |
| `verify.py` | Checks a run against the ground truth. |
| `run_all.py` | One-command demo. |

## Running pieces manually

```bash
python generate_dummy_data.py --items-per-env 100
uvicorn mock_services.environment_service:app --port 8001
uvicorn mock_services.fcc_service:app --port 8002
uvicorn mock_services.als_service:app --port 8003

python pipeline.py                         # all environments
python pipeline.py --env dc-duluth         # one environment (repeatable)
python pipeline.py --dry-run               # resolve through FCC but don't call ALS
python verify.py
```

## How the pipeline behaves

- **Classification:** a code made of exactly `UPC_LENGTH` digits counts as a UPC. Everything else counts as a local code and goes to FCC.
- **FCC lookups** are keyed on `(environment, code)` and cached, so a code that repeats within one environment costs a single call. The same local code can map to different UPCs in different environments.
- **Retries** apply only to transient failures (network errors and 5xx responses), using exponential backoff. 4xx responses are not retried.
- **Every item gets exactly one outcome:** `sent_direct`, `sent_via_fcc`, `fcc_not_found`, `fcc_error`, `als_rejected`, or `als_error`. Nothing is dropped silently.
- **Output:** `output/results.json` has one row per item. `output/summary.json` has counts per outcome and per environment.

## ADK agent (`upc_agent/`)

Wraps the pipeline's functions as Google ADK tools so an agent can run the pipeline for you. **The agent needs Python 3.10+** (a google-adk requirement). The pipeline on its own still runs on 3.9.

```bash
pip install -r requirements-agent.txt
cp upc_agent/.env.example upc_agent/.env      # add your Gemini API key or Vertex project
python run_all.py --services-only             # terminal 1: start the mock services
adk web                                       # terminal 2, from the project root; pick upc_agent
python test_agent.py                          # keyless smoke test (scripted LLM, no API calls)
```

| Tool | Writes to ALS? | What it does |
|---|---|---|
| `list_environments` | no | Lists the environments |
| `get_environment_items` | no | One page of items, each code labelled `upc` or `local` |
| `classify_code` | no | UPC or local code? |
| `resolve_upc` | no | FCC lookup for (environment, code) |
| `get_run_results` | no | Per-item results from the last run, filterable by outcome and environment |
| `check_code_status` | no | Is a UPC or local code available in ALS inventory? Local codes go through FCC first and need an environment; a UPC without one reports every environment |
| `check_environment_status` | no | Inventory counts (`in_stock` / `out_of_stock` / `not_in_inventory` / `unresolved`) for every item in an environment, plus the unavailable items |
| `send_upc_to_als` | **yes, always confirms** | Send one known UPC (e.g. a retry) |
| `process_item` | **yes, confirms when `dry_run=false`** | Full flow for one item |
| `run_pipeline` | **yes, confirms when `dry_run=false`** | Full flow for whole environments |

Tools that write to ALS use ADK's `require_confirmation`, so the agent pauses and `adk web` shows an approve/reject prompt before anything is sent. Dry runs (the default) never prompt. Every tool returns a dict with `status` set to `success`, `not_found`, or `error`.

### One-command start (Windows)

```bash
.\start.ps1          # or double-click / run start.cmd
```

Creates `.venv` on first run, starts the mock services, starts `adk web` on http://localhost:8000 and opens it (pick `upc_agent`). Ctrl+C stops everything. If an earlier run is still holding ports 8000–8003 it tells you which processes; add `-Kill` to stop them, or `-NoBrowser` to skip opening the browser. Logs are in `output/`.

### Real endpoints (`UPC_BACKEND=real`)

```bash
.\start.ps1 -Real                  # chat against the real FCC and ALS, no mock services
python test_real_endpoints.py      # read-only check of both real endpoints (needs network access)
```

Real mode is **read-only and only supports code lookups** (`check_code_status`, `resolve_upc`, `classify_code`, `list_environments`). Everything else returns an error saying it isn't supported, and nothing is ever sent to ALS.

| | Mock | Real |
|---|---|---|
| FCC | `GET /fcc/upc?environment=&code=` | `GET https://fcc-client.{environment}.tbe.zeus.fds.com/api/catalog/v2/products/{productId}` |
| ALS | `GET /als/status?upc=&environment=` | `GET http://availability-lookup-service-c1-k8s.cloudrts.net/v2/availability/divn/12/upc/{upc}?availabilityType=network&ffm=STH&country=840&channel=MCOM&pickupLocation=858` |
| UPC length | 10 | 12 |

**Unconfirmed assumptions in real mode** (all tunable in `config.py` / env vars):
- The "local code" is the FCC **product ID** (e.g. `28399242`), and the environment is the FCC hostname part (e.g. `mcore-012`, via `REAL_ENVIRONMENTS`).
- A product's UPCs are found by `pipeline.extract_upcs`, which collects 12-digit values under any key containing "upc". The one example seen had it in `unavailableUpcNumbers`; the real field for active products is unknown.
- ALS availability uses fixed `divn=12, ffm=STH, channel=MCOM, country=840, pickupLocation=858` (`ALS_*` env vars) and isn't per environment. Status is `in_stock` when ALS says `available`, otherwise `unavailable` with ALS's reason.

### Chatting with it

`adk web` is the chat interface. Type an environment name, a UPC, or a local code and the agent reports ALS inventory status. For example:

- `dc-duluth` — counts of in-stock, out-of-stock and not-in-inventory items, and which ones are unavailable
- `5868501429` — availability of that UPC in every environment
- `094777 store-atl-001` — resolves the local code through FCC, then checks ALS (local codes need the environment)

The mock ALS serves this from `GET /als/status?upc=&environment=`. Dummy inventory is generated with the rest of the data: about 60% of UPCs are in stock, 15% are known at quantity 0 (`out_of_stock`), and 25% have no record (`not_in_inventory`). Inventory is per environment.

Set `UPC_AGENT_MODEL` to change the model (default `gemini-3.5-flash`).

## Pointing it at real endpoints

Set `ENV_SERVICE_URL`, `FCC_SERVICE_URL`, and `ALS_SERVICE_URL`. Then adjust the endpoint client functions in `pipeline.py` (`get_environments`, `get_all_items`, `FCCClient._fetch`, `send_to_als`, `get_als_status`) to match the real paths, payloads, and auth headers. The rest of the pipeline stays the same.
