"""Shared configuration for the UPC pipeline POC.

Every value can be overridden with an environment variable, so pointing the
pipeline at real endpoints later is a config change, not a code change.
"""
from __future__ import annotations
import os

# --- Code format rules -------------------------------------------------------
# A code with exactly UPC_LENGTH digits is treated as a UPC and goes straight
# to ALS. Anything else is treated as an environment-specific code and must be
# resolved through FCC first.
UPC_LENGTH = int(os.getenv("UPC_LENGTH", "10"))

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
