"""Mock live-environment API.

GET /health
GET /environments                              -> ["store-atl-001", ...]
GET /environments/{env}/items?offset=0&limit=25 -> {"items": [...], "next_offset": int|null, "total": int}

Each item is {"item_id": str, "code": str}. The code is either a UPC or a
short environment-specific code; the environment doesn't say which.

Run:  uvicorn mock_services.environment_service:app --port 8001
"""
from fastapi import FastAPI, HTTPException, Query

from mock_services.common import load_data

DATA = load_data()
app = FastAPI(title="Mock Environment Service")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/environments")
def list_environments():
    return list(DATA["environments"].keys())


@app.get("/environments/{env}/items")
def list_items(env: str, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=500)):
    items = DATA["environments"].get(env)
    if items is None:
        raise HTTPException(status_code=404, detail=f"unknown environment '{env}'")
    page = items[offset: offset + limit]
    next_offset = offset + limit if offset + limit < len(items) else None
    return {"items": page, "next_offset": next_offset, "total": len(items)}
