"""Mock FCC API: resolves (environment, local code) -> UPC.

GET /health
GET /fcc/upc?environment=store-atl-001&code=123456
    200 -> {"environment": ..., "code": ..., "upc": "0123456789"}
    404 -> code not known in that environment

Run:  uvicorn mock_services.fcc_service:app --port 8002
"""
from __future__ import annotations
from fastapi import FastAPI, HTTPException, Query

from mock_services.common import load_data

DATA = load_data()
app = FastAPI(title="Mock FCC Service")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/fcc/upc")
def resolve_upc(environment: str = Query(...), code: str = Query(...)):
    env_map = DATA["fcc_mappings"].get(environment)
    if env_map is None:
        raise HTTPException(status_code=404, detail=f"unknown environment '{environment}'")
    upc = env_map.get(code)
    if upc is None:
        raise HTTPException(status_code=404, detail=f"no UPC for code '{code}' in '{environment}'")
    return {"environment": environment, "code": code, "upc": upc}
