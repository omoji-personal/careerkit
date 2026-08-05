"""Polite HTTP with caching, retries and a shared session.

Every adapter goes through here so rate-limiting, UA and timeout policy live in
one place. Responses are cached on disk for CACHE_TTL so re-running a report or
debugging a scorer does not re-hammer anyone's board.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

import requests

_local = threading.local()

# Honours CAREERKIT_HOME like store.DB_PATH and report.OUT_DIR do. Without it a
# shared-engine instance wrote its cache into the engine repo while its database
# went to its own home, which breaks the per-person isolation new-instance.sh
# exists to provide.
CACHE_DIR = Path(os.environ.get("CAREERKIT_HOME")
                 or Path(__file__).resolve().parent.parent) / "data" / "httpcache"
CACHE_TTL = 60 * 60 * 6  # 6 hours

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"})

# Per-host minimum gap between requests, seconds.
_HOST_DELAY = 0.7
_last_hit: dict[str, float] = {}


def _throttle(url: str) -> None:
    host = url.split("/")[2] if "://" in url else url
    last = _last_hit.get(host, 0.0)
    gap = time.time() - last
    if gap < _HOST_DELAY:
        time.sleep(_HOST_DELAY - gap + random.uniform(0, 0.15))
    _last_hit[host] = time.time()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


_pruned = False


def prune_cache(max_age: int = CACHE_TTL) -> int:
    """Drop cache entries past their TTL. Returns files removed.

    Nothing ever deleted expired entries, so the cache only grew: one real run
    left 734 MB across 4,913 files. Since runs are normally further apart than
    the 6h TTL, an expired entry can never be reused anyway - it is pure disk
    cost. Runs once per process, lazily, on the first fetch."""
    if not CACHE_DIR.exists():
        return 0
    cutoff = time.time() - max_age
    n = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def fetch(
    url: str,
    *,
    method: str = "GET",
    json_body: Any = None,
    headers: dict | None = None,
    timeout: int = 25,
    use_cache: bool = True,
    tries: int = 2,
) -> tuple[int, str]:
    """Return (status_code, text). Never raises on network error; returns (0, "")."""
    global _pruned
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not _pruned:
        _pruned = True
        prune_cache()
    key = f"{method}|{url}|{json.dumps(json_body, sort_keys=True) if json_body else ''}"
    cp = _cache_path(key)

    if use_cache and cp.exists() and (time.time() - cp.stat().st_mtime) < CACHE_TTL:
        try:
            blob = json.loads(cp.read_text())
            return blob["status"], blob["text"]
        except Exception:
            pass

    status, text = 0, ""
    for attempt in range(tries):
        try:
            _throttle(url)
            resp = _session.request(
                method, url, json=json_body, headers=headers or {}, timeout=timeout
            )
            status, text = resp.status_code, resp.text
            if status < 500:
                break
        except Exception:
            status, text = 0, ""
        time.sleep(1.2 * (attempt + 1))

    if use_cache and status == 200:
        try:
            cp.write_text(json.dumps({"status": status, "text": text}))
        except Exception:
            pass
    _local.last_status = status
    return status, text


def reset_status() -> None:
    """Clear the recorded status before a source runs.

    Without this a source that returns nothing WITHOUT making a request - a
    config guard, a missing tenant id, an early return - inherits whatever the
    previously polled board left behind. A healthy 200 from the last employer
    then reports the next one as "fine, nothing open" when it never ran at all."""
    _local.last_status = None


def last_status() -> int | None:
    """Status of the most recent fetch on this thread.

    run_adapter needs to tell "this board really has no openings" from "this
    board returned 403". Both produce an empty job list, and before this the
    two were reported with the identical string "0 postings returned", so every
    genuinely-empty board accumulated failures and the 'sources failing
    repeatedly' list filled with false alarms until it was worth ignoring."""
    return getattr(_local, "last_status", None)


def fetch_json(url: str, **kw) -> Any:
    status, text = fetch(url, **kw)
    if status != 200 or not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def clear_cache() -> int:
    """Drop the HTTP cache. Returns files removed."""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    return n
