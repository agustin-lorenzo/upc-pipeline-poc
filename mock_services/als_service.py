"""Mock ALS API: accepts UPCs for further processing.

GET  /health
POST /als/process  body: {"upc": str, "environment": str, "item_id": str,
                          "source_code": str, "resolved_via": "direct"|"fcc"}
     202 -> {"als_id": "...", "status": "accepted", "upc": ...}
     422 -> UPC isn't UPC_LENGTH digits
GET  /als/received -> everything ALS has accepted (for inspection/verification)
POST /als/reset    -> clears received records
GET  /als/status?upc=...&environment=...   (environment optional)
     200 -> {"upc", "environment", "available": bool, "quantity": int,
             "status": "in_stock" | "out_of_stock" | "not_in_inventory"}
            With no environment: {"upc", "available": bool (anywhere),
             "by_environment": {env: {available, quantity, status}}}
     404 -> unknown environment
     422 -> UPC isn't UPC_LENGTH digits

Run:  uvicorn mock_services.als_service:app --port 8003
"""
from __future__ import annotations
import threading
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from mock_services.common import config, load_data

INVENTORY: dict[str, dict[str, int]] = load_data()["inventory"]
app = FastAPI(title="Mock ALS Service")
_received: list[dict] = []
_lock = threading.Lock()


class ProcessRequest(BaseModel):
    upc: str
    environment: str
    item_id: str
    source_code: str
    resolved_via: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/als/process", status_code=202)
def process(req: ProcessRequest):
    if not (req.upc.isdigit() and len(req.upc) == config.UPC_LENGTH):
        raise HTTPException(status_code=422, detail=f"invalid UPC '{req.upc}'")
    record = {"als_id": str(uuid.uuid4()), "status": "accepted", **req.model_dump()}
    with _lock:
        _received.append(record)
    return {"als_id": record["als_id"], "status": "accepted", "upc": req.upc}


def _stock(environment: str, upc: str) -> dict:
    qty = INVENTORY[environment].get(upc)
    if qty is None:
        return {"available": False, "quantity": 0, "status": "not_in_inventory"}
    return {"available": qty > 0, "quantity": qty, "status": "in_stock" if qty > 0 else "out_of_stock"}


@app.get("/als/status")
def status(upc: str = Query(...), environment: Optional[str] = Query(None)):
    if not (upc.isdigit() and len(upc) == config.UPC_LENGTH):
        raise HTTPException(status_code=422, detail=f"invalid UPC '{upc}'")
    if environment is not None:
        if environment not in INVENTORY:
            raise HTTPException(status_code=404, detail=f"unknown environment '{environment}'")
        return {"upc": upc, "environment": environment, **_stock(environment, upc)}
    by_env = {env: _stock(env, upc) for env in INVENTORY}
    return {"upc": upc, "available": any(s["available"] for s in by_env.values()), "by_environment": by_env}


@app.get("/als/received")
def received():
    with _lock:
        return list(_received)


@app.post("/als/reset")
def reset():
    with _lock:
        _received.clear()
    return {"status": "cleared"}
