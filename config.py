"""Shared configuration for the UPC pipeline POC.

Every value can be overridden with an environment variable, so pointing the
pipeline at real endpoints later is a config change, not a code change.
"""
from __future__ import annotations
import os

# --- Backend -----------------------------------------------------------------
# "mock": the local FastAPI services below (default).
# "real": the real FCC and ALS read endpoints (see the REAL_* settings). Only code
#         lookups work in real mode; there's no known real endpoint for listing an
#         environment's items or for writing to ALS.
BACKEND = os.getenv("UPC_BACKEND", "mock").lower()
REAL = BACKEND == "real"

# --- Code format rules -------------------------------------------------------
# A code with exactly UPC_LENGTH digits is treated as a UPC and goes straight
# to ALS. Anything else is treated as an environment-specific code and must be
# resolved through FCC first. Real UPCs are 12 digits; the mock data uses 10.
UPC_LENGTH = int(os.getenv("UPC_LENGTH", "12" if REAL else "10"))

# --- Real endpoints (UPC_BACKEND=real) -----------------------------------------
# The environment name is part of the FCC hostname, e.g. "mcore-012". Macy's has ~23 of them;
# any name matching ENV_NAME_PATTERN is accepted (it also stops a typo or odd input from being
# turned into an arbitrary hostname). REAL_ENVIRONMENTS is just the ones we've confirmed work.
ENV_NAME_PATTERN = os.getenv("ENV_NAME_PATTERN", r"^mcore-\d{3}$")
REAL_ENVIRONMENTS = [e.strip() for e in os.getenv("REAL_ENVIRONMENTS", "mcore-012").split(",") if e.strip()]
FCC_REAL_URL_TEMPLATE = os.getenv("FCC_REAL_URL_TEMPLATE", "https://fcc-client.{environment}.tbe.zeus.fds.com")
ALS_REAL_URL = os.getenv("ALS_REAL_URL", "http://availability-lookup-service-c1-k8s.cloudrts.net")
# Defaults copied from the example ALS request; none of these are confirmed.
ALS_REAL_PARAMS = {
    "divn": os.getenv("ALS_DIVN", "12"),
    "availabilityType": os.getenv("ALS_AVAILABILITY_TYPE", "network"),
    "ffm": os.getenv("ALS_FFM", "STH"),
    "country": os.getenv("ALS_COUNTRY", "840"),
    "channel": os.getenv("ALS_CHANNEL", "MCOM"),
    "pickupLocation": os.getenv("ALS_PICKUP_LOCATION", "858"),
}

# --- Service URLs --------------------------------------------------------------
ENV_SERVICE_URL = os.getenv("ENV_SERVICE_URL", "http://127.0.0.1:8001")
FCC_SERVICE_URL = os.getenv("FCC_SERVICE_URL", "http://127.0.0.1:8002")
ALS_SERVICE_URL = os.getenv("ALS_SERVICE_URL", "http://127.0.0.1:8003")

# --- Pipeline behaviour --------------------------------------------------------
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "25"))          # items per page from an environment
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "10"))
REQUEST_TIMEOUT_S = float(os.getenv("REQUEST_TIMEOUT_S", "10"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))        # for 5xx / network errors only

# --- Files ---------------------------------------------------------------------
DATA_FILE = os.getenv("DATA_FILE", os.path.join(os.path.dirname(__file__), "data", "dummy_data.json"))
OUTPUT_DIR = os.getenv("OUTPUT_DIR", os.path.join(os.path.dirname(__file__), "output"))
