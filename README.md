# UPC Pipeline POC

Pulls item codes from live environments, resolves the ones that aren't UPCs through FCC, and sends every UPC to ALS. Ships with mock versions of all three services and dummy data so the whole thing runs locally.

```
Environment ──code──► is it a UPC (10 digits)?
                         ├─ yes ─────────────────────────► ALS
                         └─ no  ─► FCC(environment, code) ─► UPC ─► ALS
```

## Quick start

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
| `mock_services/als_service.py` | `POST /als/process`, `GET /als/received`, `POST /als/reset` |
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

## Pointing it at real endpoints

Set `ENV_SERVICE_URL`, `FCC_SERVICE_URL`, and `ALS_SERVICE_URL`. Then adjust the endpoint client functions in `pipeline.py` (`get_environments`, `get_all_items`, `FCCClient._fetch`, `send_to_als`) to match the real paths, payloads, and auth headers. The rest of the pipeline stays the same.
