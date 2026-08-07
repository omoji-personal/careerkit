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

# Say what this is. The tool tells its users it makes "ordinary outbound web
# requests" and publishes which sources are scraped; sending a Chrome string it
# is not contradicts that in the one place a site operator can actually check.
# It also gives them a way to contact or block the tool specifically, which is
# the point of the header.
UA = ("CareerKit/1.0 (open-source personal job search; "
      "+https://github.com/omoji-personal/careerkit)")

_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"})

# Per-host minimum gap between requests, seconds.
_HOST_DELAY = 0.7
_last_hit: dict[str, float] = {}
# A host that answers 429 is telling us the fixed 0.7s gap is too fast for it.
# The loop treated 429 as final (`if status < 500: break`), so it was never
# retried and the pace never changed: once a board started rate-limiting, every
# later request to it failed the same way for the rest of the run, and the
# report simply showed fewer jobs with no indication why.
_host_floor: dict[str, float] = {}
_MAX_HOST_DELAY = 30.0
# check-then-set on _last_hit was unsynchronised while cmd_verify runs 8 workers
# and discover_many up to 72, so two requests to the same host could fire inside
# the delay - breaking the politeness contract the README advertises and risking
# the rate-limiting the delay exists to avoid.
_throttle_lock = threading.Lock()


def _throttle(url: str) -> None:
    host = url.split("/")[2] if "://" in url else url
    with _throttle_lock:
        floor = _host_floor.get(host, _HOST_DELAY)
        last = _last_hit.get(host, 0.0)
        gap = time.time() - last
        wait = (floor - gap + random.uniform(0, 0.15)) if gap < floor else 0.0
        # Claim the slot before sleeping, so a second thread queues behind this
        # request rather than racing it.
        _last_hit[host] = time.time() + wait
    if wait > 0:
        time.sleep(wait)


def back_off(url: str, retry_after: str | None = None) -> float:
    """Widen this host's minimum gap after it pushes back. Returns the new gap."""
    host = url.split("/")[2] if "://" in url else url
    hinted = 0.0
    if retry_after:
        try:
            hinted = float(retry_after)
        except ValueError:
            hinted = 0.0          # HTTP-date form; the doubling below covers it
    with _throttle_lock:
        current = _host_floor.get(host, _HOST_DELAY)
        new = min(max(current * 2, hinted, _HOST_DELAY * 2), _MAX_HOST_DELAY)
        _host_floor[host] = new
    return new


def host_floors() -> dict[str, float]:
    """Hosts that asked us to slow down, and by how much. For diagnostics."""
    with _throttle_lock:
        return dict(_host_floor)


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


_pruned = False
_cache_enabled = True


def set_cache_enabled(on: bool) -> None:
    """Process-wide cache switch.

    `audit` documents itself as re-fetching live boards but, inside the 6h TTL,
    re-scored the same cached payloads. Threading use_cache through 17 adapter
    signatures would be worse than a process flag."""
    global _cache_enabled
    _cache_enabled = bool(on)


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

    use_cache = use_cache and _cache_enabled
    if use_cache and cp.exists() and (time.time() - cp.stat().st_mtime) < CACHE_TTL:
        try:
            blob = json.loads(cp.read_text())
            # Record the status on this path too. Only 200s are ever cached, so
            # a cache hit IS a success - but returning without setting it left
            # the thread-local holding a previous board's value (or None after
            # reset_status), and run_adapter then blamed a perfectly healthy
            # cached board for someone else's failure.
            _local.last_status = blob["status"]
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
            if status == 429:
                # Do NOT retry. A host answering 429 is asking for less traffic,
                # and an immediate second attempt is the opposite of that. What
                # was actually broken is that the pace never changed: the fixed
                # 0.7s gap was reused for every later request to the same host,
                # so once a board started limiting, the rest of the run kept
                # hitting it at the pace it had just refused. Widen this host's
                # floor instead, honouring its own Retry-After when it sends one.
                back_off(url, getattr(resp, "headers", {}).get("Retry-After"))
                break
            if status < 500:
                break
        except Exception:
            status, text = 0, ""
        if attempt + 1 < tries:      # never sleep after the last attempt
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
    _local.last_parse_ok = None


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
    _local.last_parse_ok = None
    if status != 200 or not text:
        return None
    try:
        d = json.loads(text)
        _local.last_parse_ok = True
        return d
    except Exception:
        # A 200 whose body is not JSON is a WAF challenge, an SSO redirect or a
        # changed API - never "no openings". Recording it is what stops the
        # board from dropping out of coverage silently.
        _local.last_parse_ok = False
        return None


def last_parse_ok() -> bool | None:
    """True/False for the most recent fetch_json on this thread, None if the
    last fetch did not go through fetch_json at all."""
    return getattr(_local, "last_parse_ok", None)


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
