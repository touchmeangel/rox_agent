# rox_core/results.py
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from rox_core.cli_common import aggregate_usage
from rox_core.serialization import to_jsonable
from rox_core.steps.base import StepResult

RESULT_URL_ENV = "ROX_RESULT_PUT_URL"


def results_payload(results: dict[str, StepResult], **extra) -> dict:
    return {
        "generated_at": datetime.now().isoformat(),
        **extra,
        "steps": {
            name: {
                "success": r.success,
                "iterations": r.iterations_used,
                "summary": r.summary,
                "findings": r.findings,
                "plan": r.plan,
                "blockers": r.blockers,
                "model_usage": r.model_usage,
            }
            for name, r in results.items()
        },
        "usage_summary": aggregate_usage(results),
    }


def publish_results(payload: dict, output_path: str | None) -> bool:
    body = json.dumps(to_jsonable(payload), indent=2).encode()

    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)

    url = os.environ.get(RESULT_URL_ENV)
    if not url:
        return True
    return _put_with_retry(url, body)


def _put_with_retry(url: str, body: bytes, attempts: int = 4) -> bool:
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(  # noqa: S310
            url, data=body, method="PUT", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120):  # noqa: S310
                return True
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                print(f"rox: result upload rejected (HTTP {exc.code})", file=sys.stderr)
                return False
            err = f"HTTP {exc.code}"
        except OSError as exc:
            err = repr(exc)
        print(
            f"rox: result upload attempt {attempt}/{attempts} failed: {err}",
            file=sys.stderr,
        )
        if attempt < attempts:
            time.sleep(2**attempt)
    return False
