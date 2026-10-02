"""HTTP smoke test for the wal-recover service.

Exercises /health and /recover over real HTTP against the service named by
APP_URL (default http://127.0.0.1:8080).  Prints one line per check and
exits non-zero if any check fails.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.walfixture import (  # noqa: E402
    WalBuilder,
    continue_wal,
    make_real_db_and_wal,
    minimal_db,
)

APP = os.environ.get("APP_URL", "http://127.0.0.1:8080").rstrip("/")

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {name}"
    if detail and not condition:
        line += f" -- {detail}"
    print(line, flush=True)
    if not condition:
        FAILURES.append(name)


def request(method, path, payload=None, raw_body=None):
    data = raw_body
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(APP + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def wait_healthy(attempts=60):
    for _ in range(attempts):
        try:
            status, body = request("GET", "/health")
            if status == 200 and body.get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main():
    print(f"smoke against {APP}", flush=True)
    check("health endpoint", wait_healthy())

    tmp = tempfile.mkdtemp(prefix="wal-smoke-")
    db, wal, meta = make_real_db_and_wal(tmp, page_size=1024, txns=3)
    garbage = b"\xee" * 1024
    wal = continue_wal(wal, [(1, 0, garbage), (2, 0, garbage)])
    payload = {
        "database": base64.b64encode(db).decode("ascii"),
        "wal": base64.b64encode(wal).decode("ascii"),
        "stable_page_order": True,
    }

    # 1. valid multi-transaction WAL --------------------------------------
    status, body = request("POST", "/recover", payload)
    check("valid WAL returns 200", status == 200, f"got {status}: {body}")
    ok = status == 200 and body.get("ok") is True
    check("valid WAL ok flag", ok)
    if ok:
        check(
            "commit frame is last real commit",
            body["commit_frame"] == meta["frames"],
            f"commit_frame={body.get('commit_frame')} expected={meta['frames']}",
        )
        check(
            "recovered page count matches page list",
            body["recovered_pages"] == len(body["pages"]),
        )
        pages = [p["page"] for p in body["pages"]]
        check("pages sorted by page number (stable order)", pages == sorted(pages))
        image = base64.b64decode(body["image"])
        check(
            "image size consistent",
            body["image_size"] == len(image) == body["commit_size_pages"] * 1024,
        )
        check(
            "image digest matches",
            body["image_sha256"] == hashlib.sha256(image).hexdigest(),
        )
        check("uncommitted tail excluded", image[:1024] != garbage)
        path = os.path.join(tmp, "recovered.db")
        with open(path, "wb") as fh:
            fh.write(image)
        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT count(*) FROM t").fetchone()[0]
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()
        check("recovered image holds committed rows", rows == meta["rows"])
        check("recovered image passes integrity_check", integrity == "ok")

    # 2. stable_page_order=false keeps application order -------------------
    payload["stable_page_order"] = False
    status, body = request("POST", "/recover", payload)
    ok = status == 200 and body.get("ok") is True
    check("unordered request returns 200", ok)
    if ok:
        keys = [(p["source_frame"], p["page"]) for p in body["pages"]]
        check("pages in application order", keys == sorted(keys))

    # 3. corrupted last frame ----------------------------------------------
    bad = bytearray(wal)
    bad[-1] ^= 0xFF
    last_frame_offset = len(wal) - (24 + 1024)
    payload["wal"] = base64.b64encode(bytes(bad)).decode("ascii")
    status, body = request("POST", "/recover", payload)
    check("corrupted last frame returns 422", status == 422, f"got {status}")
    check(
        "corrupted frame error code",
        body.get("error", {}).get("code") == "checksum_mismatch",
        str(body),
    )
    check(
        "corrupted frame offset located",
        body.get("error", {}).get("offset") == last_frame_offset,
        str(body),
    )
    check("no partial image on corruption", "image" not in body)

    # 4. truncated frame ----------------------------------------------------
    payload["wal"] = base64.b64encode(wal[:-10]).decode("ascii")
    status, body = request("POST", "/recover", payload)
    check(
        "truncated frame reported",
        status == 422 and body.get("error", {}).get("code") == "truncated_frame",
        f"{status}: {body}",
    )
    check("no partial image on truncation", "image" not in body)

    # 5. WAL without any commit frame ---------------------------------------
    nocommit = WalBuilder(1024).add_frame(1, 0).add_frame(2, 0).bytes()
    payload["wal"] = base64.b64encode(nocommit).decode("ascii")
    status, body = request("POST", "/recover", payload)
    check(
        "no-commit WAL reported",
        status == 422 and body.get("error", {}).get("code") == "no_commit",
        f"{status}: {body}",
    )
    check("no partial image without commit", "image" not in body)

    # 6. malformed requests ---------------------------------------------------
    status, body = request("POST", "/recover", {"database": "!!!", "wal": "%%%"})
    check(
        "invalid base64 rejected",
        status == 400 and body.get("error", {}).get("code") == "invalid_base64",
        f"{status}: {body}",
    )
    status, body = request("POST", "/recover", raw_body=b"not json")
    check(
        "invalid JSON rejected",
        status == 400 and body.get("error", {}).get("code") == "invalid_json",
        f"{status}: {body}",
    )
    status, body = request("POST", "/recover", {"database": "", "wal": ""})
    check(
        "empty WAL has no commit",
        status == 422 and body.get("error", {}).get("code") == "invalid_wal_header",
        f"{status}: {body}",
    )

    # 7. oversized WAL --------------------------------------------------------
    payload = {
        "database": base64.b64encode(minimal_db(1024)).decode("ascii"),
        "wal": base64.b64encode(b"\x00" * (2 * 1024 * 1024 + 1)).decode("ascii"),
    }
    status, body = request("POST", "/recover", payload)
    check(
        "oversized WAL rejected",
        status == 422 and body.get("error", {}).get("code") == "wal_too_large",
        f"{status}: {body}",
    )

    # 8. unknown endpoint -----------------------------------------------------
    status, _ = request("GET", "/nope")
    check("unknown endpoint returns 404", status == 404)

    print(flush=True)
    if FAILURES:
        print(f"SMOKE FAILED: {len(FAILURES)} check(s) failed: {FAILURES}", flush=True)
        return 1
    print("SMOKE OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
