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
import time
from pathlib import Path
from typing import Any

import requests

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "httpcache"
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
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
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
    return status, text


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
