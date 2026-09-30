"""One-command demo: generate data, start the three mock services, run the
pipeline, verify the results, and shut everything down.

Usage:
  python run_all.py                  # full run
  python run_all.py --keep-running   # leave the mock services up afterwards
  python run_all.py --seed 7 --items-per-env 200
"""
from __future__ import annotations
import argparse
import asyncio
import os
import subprocess
import sys
import time
from urllib.parse import urlparse

import httpx

import config

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVICES = [
    ("environment", "mock_services.environment_service:app", config.ENV_SERVICE_URL),
    ("fcc", "mock_services.fcc_service:app", config.FCC_SERVICE_URL),
    ("als", "mock_services.als_service:app", config.ALS_SERVICE_URL),
]


def start_services() -> list[subprocess.Popen]:
    procs = []
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    for name, target, url in SERVICES:
        u = urlparse(url)
        log = open(os.path.join(ROOT, "output", f"{name}_service.log"), "w")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target, "--host", u.hostname, "--port", str(u.port),
             "--log-level", "warning"],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        ))
        print(f"Started {name:<12} at {url}")
    return procs


def wait_healthy(timeout_s: float = 15) -> None:
    deadline = time.time() + timeout_s
    pending = {name: url for name, _, url in SERVICES}
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    pending.pop(name)
            except httpx.TransportError:
                pass
        time.sleep(0.2)
    if pending:
        raise SystemExit(f"Services failed to start: {list(pending)} (see output/*_service.log)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--items-per-env", type=int, default=40)
    parser.add_argument("--keep-running", action="store_true")
    parser.add_argument("--services-only", action="store_true",
                        help="start the mock services and wait (e.g. for the ADK agent); no pipeline run")
    args = parser.parse_args()

    subprocess.run([sys.executable, "generate_dummy_data.py", "--seed", str(args.seed),
                    "--items-per-env", str(args.items_per_env)], cwd=ROOT, check=True)

    procs = start_services()
    ok = False
    try:
        wait_healthy()
        httpx.post(f"{config.ALS_SERVICE_URL}/als/reset", timeout=5)

        if args.services_only:
            ok = True
            print("\nMock services running. Ctrl+C to stop.")
            while True:
                time.sleep(1)

        import logging
        import pipeline
        import verify
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
        logging.getLogger("httpx").setLevel(logging.WARNING)

        pipeline.print_summary(asyncio.run(pipeline.run()))
        ok = verify.verify()

        if args.keep_running:
            print("\nServices still running. Ctrl+C to stop.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(timeout=5)
        print("Services stopped.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
