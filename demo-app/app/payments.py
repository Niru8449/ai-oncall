import os

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.common import env_int, setup_logging

log = setup_logging("payments-gw")

PAYMENTS_FAIL = os.environ.get("PAYMENTS_FAIL", "0") == "1"
PORT = env_int("PORT", 8000)

app = FastAPI()


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.post("/charge")
async def charge():
    if PAYMENTS_FAIL:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "approved"}


def run():
    log.info("payments-gw started fail=%s", PAYMENTS_FAIL)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning", access_log=False)
