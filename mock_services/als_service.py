"""Mock ALS API: accepts UPCs for further processing.

GET  /health
POST /als/process  body: {"upc": str, "environment": str, "item_id": str,
                          "source_code": str, "resolved_via": "direct"|"fcc"}
     202 -> {"als_id": "...", "status": "accepted", "upc": ...}
     422 -> UPC isn't UPC_LENGTH digits
GET  /als/received -> everything ALS has accepted (for inspection/verification)
POST /als/reset    -> clears received records

Run:  uvicorn mock_services.als_service:app --port 8003
"""
import threading
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mock_services.common import config

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


@app.get("/als/received")
def received():
    with _lock:
        return list(_received)


@app.post("/als/reset")
def reset():
    with _lock:
        _received.clear()
    return {"status": "cleared"}
