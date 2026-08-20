"""Polite HTTP with caching, retries and a shared session.

Every adapter goes through here so rate-limiting, UA and timeout policy live in
one place. Responses are cached on disk for CACHE_TTL so re-running a report or
debugging a scorer does not re-hammer anyone's board.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

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

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_SENSITIVE_HEADER_PARTS = frozenset({
    "api", "auth", "authorization", "cookie", "credential", "credentials",
    "email", "key", "passwd", "password", "secret", "session", "signature",
    "token",
})
_CROSS_ORIGIN_HEADER_ALLOWLIST = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control",
    "content-type", "range",
})
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")
_NAT64_WELL_KNOWN = ipaddress.IPv6Network("64:ff9b::/96")


class UnsafeExternalURL(ValueError):
    """A URL is not safe for a server-side fetch.

    This is deliberately distinct from a network error. Callers that accept a
    URL from an external feed should report that it was refused, rather than
    disguising a policy decision as a timeout.
    """


def _globally_routable(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Stricter than is_global, which labels multicast/site-local as global."""
    flags = (
        address.is_private,
        address.is_loopback,
        address.is_link_local,
        address.is_multicast,
        address.is_reserved,
        address.is_unspecified,
        getattr(address, "is_site_local", False),
    )
    return address.is_global and not any(flags)


def _public_ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse one resolver result and reject local/special-use destinations."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError as exc:
        raise UnsafeExternalURL(f"resolver returned an invalid address: {address!r}") from exc
    if not _globally_routable(ip):
        raise UnsafeExternalURL(f"destination resolves to non-public address {ip}")

    # Some globally-labelled IPv6 transition ranges embed an IPv4 destination.
    # Check the embedded address too, otherwise e.g. a 6to4 representation of a
    # loopback address can pass an IPv6-only is_global check.
    embedded: list[ipaddress.IPv4Address] = []
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour:
            embedded.append(ip.sixtofour)
        if ip.teredo:
            embedded.extend(ip.teredo)
        if ip in _IPV4_COMPATIBLE or ip in _NAT64_WELL_KNOWN:
            embedded.append(ipaddress.IPv4Address(int(ip) & 0xffffffff))
    for inner in embedded:
        if not _globally_routable(inner):
            raise UnsafeExternalURL(
                f"destination embeds non-public address {inner} in {ip}"
            )
    return ip


def validate_public_url(url: str) -> tuple[str, ...]:
    """Validate an HTTP(S) URL and return every currently resolved public IP.

    The check is intentionally conservative: if a hostname has a mixture of
    public and private answers, the whole destination is refused because the
    HTTP client's address selection is not under the caller's control.
    """
    if not isinstance(url, str) or not url or "\\" in url:
        raise UnsafeExternalURL("URL must be a non-empty HTTP(S) URL")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in url):
        raise UnsafeExternalURL("URL contains control characters")
    if any(unicodedata.category(ch) == "Cf" for ch in url):
        raise UnsafeExternalURL("URL contains invisible format characters")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeExternalURL(f"malformed URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeExternalURL("only http and https URLs may be fetched")
    if not parsed.hostname:
        raise UnsafeExternalURL("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeExternalURL("credential-bearing URLs are not allowed")

    service_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        answers = socket.getaddrinfo(
            parsed.hostname, service_port, type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise UnsafeExternalURL(
            f"hostname could not be resolved: {parsed.hostname}"
        ) from exc
    resolved = tuple(dict.fromkeys(a[4][0] for a in answers if a[4]))
    if not resolved:
        raise UnsafeExternalURL(f"hostname has no usable address: {parsed.hostname}")
    # Return canonical numeric strings. The pinned transport consumes these
    # directly, so neither a hostname nor a scoped/alternate IP spelling can
    # reach the connection layer and trigger a second authoritative DNS lookup.
    addresses = tuple(dict.fromkeys(str(_public_ip(address)) for address in resolved))
    return addresses


def _verified_ssl_context() -> ssl.SSLContext:
    """Build the verified context used by the direct, pinned TLS transport."""
    bundle = (os.environ.get("REQUESTS_CA_BUNDLE")
              or os.environ.get("CURL_CA_BUNDLE")
              or requests.certs.where())
    if os.path.isdir(bundle):
        return ssl.create_default_context(capath=bundle)
    return ssl.create_default_context(cafile=bundle)


def _new_pinned_pool(
    scheme: str, address: str, port: int, tls_hostname: str,
):
    """Create a one-destination pool whose TCP peer is a validated numeric IP."""
    if scheme == "https":
        return urllib3.HTTPSConnectionPool(
            host=address,
            port=port,
            maxsize=1,
            block=True,
            cert_reqs=ssl.CERT_REQUIRED,
            assert_hostname=tls_hostname,
            server_hostname=tls_hostname,
            ssl_context=_verified_ssl_context(),
        )
    return urllib3.HTTPConnectionPool(
        host=address, port=port, maxsize=1, block=True,
    )


def _pinned_headers(
    supplied: dict[str, str], host_header: str,
) -> dict[str, str]:
    """Merge harmless session defaults, then force Host to the original name."""
    merged = requests.structures.CaseInsensitiveDict()
    for name, value in _session.headers.items():
        lower = str(name).lower()
        if (lower in {"host", "cookie", "origin", "referer"}
                or _credential_like_header(str(name))):
            continue
        merged[str(name)] = str(value)
    for name, value in supplied.items():
        if value is not None and str(name).lower() != "host":
            merged[str(name)] = str(value)
    merged["Host"] = host_header
    return dict(merged)


def _response_from_urllib3(raw, url: str) -> requests.Response:
    """Copy a preloaded urllib3 response into the small Requests interface used here."""
    response = requests.Response()
    response.status_code = raw.status
    response.headers = requests.structures.CaseInsensitiveDict(raw.headers)
    data = raw.data
    response._content = data.encode() if isinstance(data, str) else bytes(data or b"")
    response._content_consumed = True
    response.url = url
    response.encoding = requests.utils.get_encoding_from_headers(response.headers)
    return response


def _pinned_request(
    method: str,
    url: str,
    *,
    addresses: tuple[str, ...],
    json_body: Any,
    headers: dict[str, str],
    timeout: int,
) -> requests.Response:
    """Connect to a validated IP while authenticating the original HTTPS name.

    A proxy cannot provide this property because it normally resolves the
    hostname itself, so safe-external requests intentionally use a direct pool.
    The ordinary fetch path remains on the shared Requests session.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").rstrip(".")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        tls_hostname = hostname.encode("idna").decode("ascii").lower()
        host_name = tls_hostname
    else:
        tls_hostname = str(literal)
        host_name = f"[{tls_hostname}]" if literal.version == 6 else tls_hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    host_header = host_name + (f":{parsed.port}" if parsed.port is not None else "")
    request_headers = _pinned_headers(headers, host_header)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        if not any(str(name).lower() == "content-type" for name in request_headers):
            request_headers["Content-Type"] = "application/json"
    target = requests.utils.requote_uri(parsed.path or "/")
    if parsed.query:
        target += "?" + requests.utils.requote_uri(parsed.query)

    last_error: Exception | None = None
    candidates = addresses if method.upper() in {"GET", "HEAD"} else addresses[:1]
    for address in candidates:
        pool = _new_pinned_pool(scheme, address, port, tls_hostname)
        try:
            raw = pool.urlopen(
                method.upper(),
                target,
                body=body,
                headers=request_headers,
                retries=False,
                redirect=False,
                timeout=timeout,
                preload_content=True,
                decode_content=True,
            )
            return _response_from_urllib3(raw, url)
        except Exception as exc:
            last_error = exc
        finally:
            pool.close()
    if last_error is not None:
        raise last_error
    raise UnsafeExternalURL("hostname has no validated address")


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), (parsed.hostname or "").lower().rstrip("."), port


def _credential_like_header(name: str) -> bool:
    lower = name.lower()
    parts = set(re.findall(r"[a-z0-9]+", lower))
    compact = re.sub(r"[^a-z0-9]", "", lower)
    return bool(parts & _SENSITIVE_HEADER_PARTS) or any(marker in compact for marker in (
        "accesstoken", "apikey", "authorization", "authtoken", "credential",
        "password", "secret", "sessionid", "signature",
    ))


def _headers_after_redirect(
    headers: dict[str, str], old_url: str, new_url: str,
) -> dict[str, str]:
    """Copy request headers, dropping stale routing and cross-origin secrets."""
    cross_origin = _origin(old_url) != _origin(new_url)
    out: dict[str, str] = {}
    for name, value in headers.items():
        lower = str(name).lower()
        # requests must calculate Host for the new destination. Origin and
        # Referer are likewise unsafe to carry as caller-supplied stale values.
        if lower in {"host", "origin", "referer"}:
            continue
        # A cross-origin hop gets a small allowlist, rather than an inevitably
        # incomplete denylist of secret header names. In particular this drops
        # USAJobs' email-as-User-Agent and custom Authorization-Key headers.
        if cross_origin and (
            lower not in _CROSS_ORIGIN_HEADER_ALLOWLIST
            or _credential_like_header(str(name))
        ):
            continue
        out[name] = value
    return out


def _request_following_redirects(
    method: str,
    url: str,
    *,
    json_body: Any,
    headers: dict[str, str],
    timeout: int,
    safe_external: bool,
    max_redirects: int,
    initial_addresses: tuple[str, ...] | None = None,
):
    """Make one request attempt while retaining control of every redirect."""
    current_url = url
    current_method = method.upper()
    current_body = json_body
    current_headers = dict(headers)
    current_addresses = initial_addresses

    for redirect_count in range(max_redirects + 1):
        if safe_external:
            # Resolve once for this hop and hand only those numeric addresses to
            # the connection pool. Retries resolve afresh; redirects validate
            # their own destination before any connection is opened.
            current_addresses = current_addresses or validate_public_url(current_url)
        _throttle(current_url)
        if safe_external:
            resp = _pinned_request(
                current_method,
                current_url,
                addresses=current_addresses or (),
                json_body=current_body,
                headers=current_headers,
                timeout=timeout,
            )
        else:
            resp = _session.request(
                current_method,
                current_url,
                json=current_body,
                headers=current_headers,
                timeout=timeout,
                allow_redirects=False,
            )
        response_headers = getattr(resp, "headers", {}) or {}
        location = response_headers.get("Location") or response_headers.get("location")
        if resp.status_code not in _REDIRECT_STATUSES or not location:
            return resp, current_url
        if redirect_count >= max_redirects:
            close = getattr(resp, "close", None)
            if callable(close):
                close()
            raise requests.TooManyRedirects(f"more than {max_redirects} redirects")

        next_url = urljoin(current_url, location)
        old_scheme = urlsplit(current_url).scheme.lower()
        new_scheme = urlsplit(next_url).scheme.lower()
        if old_scheme == "https" and new_scheme != "https":
            close = getattr(resp, "close", None)
            if callable(close):
                close()
            raise UnsafeExternalURL("refusing an HTTPS downgrade redirect")
        next_addresses = validate_public_url(next_url) if safe_external else None

        cross_origin = _origin(current_url) != _origin(next_url)
        if cross_origin and current_body is not None and resp.status_code in {307, 308}:
            close = getattr(resp, "close", None)
            if callable(close):
                close()
            raise UnsafeExternalURL(
                "refusing to forward a request body across origins"
            )

        current_headers = _headers_after_redirect(
            current_headers, current_url, next_url
        )
        if (resp.status_code == 303 and current_method != "HEAD") or (
            resp.status_code in {301, 302} and current_method not in {"GET", "HEAD"}
        ):
            current_method = "GET"
            current_body = None
            current_headers = {
                k: v for k, v in current_headers.items()
                if str(k).lower() not in {"content-length", "content-type"}
            }
        close = getattr(resp, "close", None)
        if callable(close):
            close()
        current_url = next_url
        current_addresses = next_addresses

    raise requests.TooManyRedirects(f"more than {max_redirects} redirects")


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
    re-scored the same cached payloads. Threading use_cache through every adapter
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
    safe_external: bool = False,
    max_redirects: int = _MAX_REDIRECTS,
) -> tuple[int, str]:
    """Return ``(status_code, text)`` for a bounded HTTP request.

    Network errors return ``(0, "")``. When ``safe_external`` is true, the URL
    and every redirect must resolve exclusively to public addresses; a refused
    destination raises :class:`UnsafeExternalURL` so callers can distinguish a
    security decision from an outage.
    """
    global _pruned
    initial_addresses: tuple[str, ...] | None = None
    if safe_external:
        # Validate before looking in the cache too. A URL that is no longer a
        # permitted destination must not become accepted merely because an old
        # response happens to be present on disk.
        try:
            initial_addresses = validate_public_url(url)
        except UnsafeExternalURL:
            _local.last_status = 0
            raise
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not _pruned:
        _pruned = True
        prune_cache()
    # A response fetched through the ordinary Requests path is not evidence that
    # every redirect was public or that the connection was pinned to a validated
    # address. Keep the hardened transport in its own versioned namespace so a
    # pre-policy/ordinary cache entry can never satisfy a safe-external request.
    # Bump the version if the outbound validation contract changes materially.
    cache_policy = "safe-external-v1|" if safe_external else ""
    key = (f"{cache_policy}{method}|{url}|"
           f"{json.dumps(json_body, sort_keys=True) if json_body else ''}")
    cp = _cache_path(key)

    use_cache = use_cache and _cache_enabled
    if use_cache and cp.exists() and (time.time() - cp.stat().st_mtime) < CACHE_TTL:
        try:
            blob = json.loads(cp.read_text(encoding="utf-8"))
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
            resp, final_url = _request_following_redirects(
                method,
                url,
                json_body=json_body,
                headers=headers or {},
                timeout=timeout,
                safe_external=safe_external,
                max_redirects=max_redirects,
                initial_addresses=initial_addresses if attempt == 0 else None,
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
                back_off(final_url, getattr(resp, "headers", {}).get("Retry-After"))
                break
            if status < 500:
                break
        except UnsafeExternalURL:
            _local.last_status = 0
            raise
        except Exception:
            status, text = 0, ""
        if attempt + 1 < tries:      # never sleep after the last attempt
            time.sleep(1.2 * (attempt + 1))

    if use_cache and status == 200:
        try:
            cp.write_text(
                json.dumps({"status": status, "text": text}),
                encoding="utf-8",
            )
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
