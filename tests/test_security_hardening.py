"""Focused security regressions. All HTTP and DNS behavior is simulated."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import aggregators, http, jd
from engine.models import Job, sanitize_external, sanitize_external_url


class _Response:
    def __init__(self, status: int, text: str = "", headers: dict | None = None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class _RawResponse:
    def __init__(self, status: int, data: bytes = b"", headers: dict | None = None):
        self.status = status
        self.data = data
        self.headers = headers or {}


class _PinnedPool:
    def __init__(self, response: _RawResponse):
        self.response = response
        self.calls = []
        self.closed = False

    def urlopen(self, method, target, **kwargs):
        self.calls.append((method, target, kwargs))
        return self.response

    def close(self):
        self.closed = True


def _answer(address: str, port: int = 443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


@pytest.fixture
def isolated_http(tmp_path, monkeypatch):
    """Prevent cache/throttle state from making a network-policy test impure."""
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(http, "_pruned", True)
    monkeypatch.setattr(http, "_throttle", lambda _url: None)


def test_canonical_fetch_refuses_loopback_before_request(isolated_http, monkeypatch):
    requested = []
    monkeypatch.setattr(
        http.socket,
        "getaddrinfo",
        lambda host, port, **_kw: [_answer("127.0.0.1", port)],
    )
    monkeypatch.setattr(
        http._session, "request", lambda *a, **k: requested.append((a, k))
    )

    text, reason = jd.fetch_canonical("http://127.0.0.1/private")

    assert text == ""
    assert reason.startswith("refused unsafe URL:")
    assert "non-public" in reason
    assert requested == [], "policy validation must happen before a socket is opened"


def test_linkedin_text_in_a_local_path_does_not_bypass_url_policy(
    isolated_http, monkeypatch,
):
    requested = []
    monkeypatch.setattr(
        http.socket,
        "getaddrinfo",
        lambda host, port, **_kw: [_answer("127.0.0.1", port)],
    )
    monkeypatch.setattr(
        http._session, "request", lambda *a, **k: requested.append((a, k))
    )

    text, reason = jd.fetch_canonical(
        "http://127.0.0.1/linkedin.com/jobs/view/fake-123456"
    )

    assert text == "" and "refused unsafe URL" in reason
    assert requested == []


def test_safe_fetch_rejects_mixed_public_private_dns(isolated_http, monkeypatch):
    monkeypatch.setattr(
        http.socket,
        "getaddrinfo",
        lambda host, port, **_kw: [
            _answer("93.184.216.34", port),
            _answer("10.20.30.40", port),
        ],
    )
    monkeypatch.setattr(
        http._session,
        "request",
        lambda *_a, **_k: pytest.fail("mixed DNS answers must be refused"),
    )
    http._local.last_status = 200

    with pytest.raises(http.UnsafeExternalURL, match="non-public"):
        http.fetch("https://mixed.example/job", safe_external=True, use_cache=False)
    assert http.last_status() == 0


def test_safe_fetch_never_consumes_an_ordinary_or_legacy_cache_entry(
    isolated_http, monkeypatch,
):
    url = "https://public.example/job"
    # This is the exact unversioned key used by ordinary fetches and by builds
    # predating redirect validation/IP pinning.
    legacy_path = http._cache_path(f"GET|{url}|")
    legacy_path.write_text(json.dumps({"status": 200, "text": "unverified"}))

    monkeypatch.setattr(
        http.socket, "getaddrinfo",
        lambda host, port, **_kw: [_answer("93.184.216.34", port)],
    )
    calls = []

    def verified_request(method, request_url, **kwargs):
        calls.append((method, request_url, kwargs["addresses"]))
        return _Response(200, "verified-safe-response")

    monkeypatch.setattr(http, "_pinned_request", verified_request)

    status, text = http.fetch(
        url, safe_external=True, use_cache=True, tries=1,
    )

    assert (status, text) == (200, "verified-safe-response")
    assert calls == [("GET", url, ("93.184.216.34",))]
    assert legacy_path.exists()
    assert len(list(http.CACHE_DIR.glob("*.json"))) == 2


def test_safe_fetch_validates_redirect_destination_before_following(
    isolated_http, monkeypatch,
):
    resolutions = []

    def fake_dns(host, port, **_kw):
        resolutions.append(host)
        address = "10.0.0.8" if host == "internal.example" else "93.184.216.34"
        return [_answer(address, port)]

    requests = []

    def fake_request(method, url, **kwargs):
        requests.append((method, url, kwargs))
        return _Response(302, headers={"Location": "https://internal.example/admin"})

    monkeypatch.setattr(http.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(http, "_pinned_request", fake_request)

    with pytest.raises(http.UnsafeExternalURL, match="non-public"):
        http.fetch("https://public.example/job", safe_external=True, use_cache=False)

    assert [item[1] for item in requests] == ["https://public.example/job"]
    assert requests[0][2]["addresses"] == ("93.184.216.34",)
    assert "internal.example" in resolutions


def test_safe_https_pins_tcp_ip_but_keeps_original_tls_and_host(
    isolated_http, monkeypatch,
):
    dns_calls = []
    connections = []
    pool = _PinnedPool(_RawResponse(
        200, b"pinned", {"Content-Type": "text/plain; charset=utf-8"},
    ))

    def fake_dns(host, port, **_kw):
        dns_calls.append((host, port))
        return [_answer("93.184.216.34", port)]

    def fake_pool(scheme, address, port, tls_hostname):
        connections.append((scheme, address, port, tls_hostname))
        return pool

    monkeypatch.setattr(http.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(http, "_new_pinned_pool", fake_pool)
    monkeypatch.setattr(
        http._session, "request",
        lambda *_a, **_k: pytest.fail("safe fetch must not use the DNS-based session"),
    )

    status, text = http.fetch(
        "https://Jobs.Example:8443/posting?id=7",
        safe_external=True, use_cache=False, tries=1,
    )

    assert (status, text) == (200, "pinned")
    assert dns_calls == [("jobs.example", 8443)]
    assert connections == [("https", "93.184.216.34", 8443, "jobs.example")]
    method, target, kwargs = pool.calls[0]
    assert (method, target) == ("GET", "/posting?id=7")
    assert kwargs["headers"]["Host"] == "jobs.example:8443"
    assert kwargs["headers"]["User-Agent"] == http.UA
    assert kwargs["redirect"] is False and kwargs["retries"] is False
    assert pool.closed


def test_pinned_https_pool_verifies_certificate_for_original_name(monkeypatch):
    captured = {}
    marker_context = object()
    marker_pool = object()

    def fake_https_pool(**kwargs):
        captured.update(kwargs)
        return marker_pool

    monkeypatch.setattr(http, "_verified_ssl_context", lambda: marker_context)
    monkeypatch.setattr(http.urllib3, "HTTPSConnectionPool", fake_https_pool)

    made = http._new_pinned_pool(
        "https", "93.184.216.34", 443, "jobs.example"
    )

    assert made is marker_pool
    assert captured["host"] == "93.184.216.34"
    assert captured["server_hostname"] == "jobs.example"  # TLS SNI
    assert captured["assert_hostname"] == "jobs.example"  # certificate SAN/CN
    assert captured["cert_reqs"] == http.ssl.CERT_REQUIRED
    assert captured["ssl_context"] is marker_context


def test_safe_redirect_repins_each_host_and_drops_cross_host_secrets(
    isolated_http, monkeypatch,
):
    addresses = {
        "start.example": "93.184.216.34",
        "next.example": "142.250.72.14",
    }
    dns_calls = []
    connections = []
    pools = []

    def fake_dns(host, port, **_kw):
        dns_calls.append(host)
        return [_answer(addresses[host], port)]

    def fake_pool(scheme, address, port, tls_hostname):
        response = (_RawResponse(
            302, headers={"Location": "https://next.example/final"},
        ) if tls_hostname == "start.example" else _RawResponse(200, b"done"))
        pool = _PinnedPool(response)
        pools.append(pool)
        connections.append((address, tls_hostname))
        return pool

    monkeypatch.setattr(http.socket, "getaddrinfo", fake_dns)
    monkeypatch.setattr(http, "_new_pinned_pool", fake_pool)
    monkeypatch.setattr(
        http._session, "request",
        lambda *_a, **_k: pytest.fail("redirected safe fetch must stay pinned"),
    )

    status, text = http.fetch(
        "https://start.example/job",
        headers={"Authorization": "Bearer secret", "XApiKey": "secret"},
        safe_external=True, use_cache=False, tries=1,
    )

    assert (status, text) == (200, "done")
    assert dns_calls == ["start.example", "next.example"]
    assert connections == [
        ("93.184.216.34", "start.example"),
        ("142.250.72.14", "next.example"),
    ]
    first_headers = pools[0].calls[0][2]["headers"]
    second_headers = pools[1].calls[0][2]["headers"]
    assert first_headers["Host"] == "start.example"
    assert first_headers["Authorization"] == "Bearer secret"
    assert second_headers["Host"] == "next.example"
    assert "Authorization" not in second_headers and "XApiKey" not in second_headers


def test_cross_origin_redirect_drops_credentials_and_personal_headers(
    isolated_http, monkeypatch,
):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if len(calls) == 1:
            return _Response(302, headers={"Location": "https://other.example/next"})
        return _Response(200, "ok")

    monkeypatch.setattr(http._session, "request", fake_request)
    status, text = http.fetch(
        "https://api.example/start",
        headers={
            "Host": "api.example",
            "Authorization": "Bearer secret",
            "Authorization-Key": "secret-key",
            "Cookie": "session=secret",
            "X-Api-Token": "secret-token",
            "XApiKey": "compact-secret",
            "User-Agent": "owner@example.com",
            "X-Trace-Id": "safe-correlation",
            "Accept": "application/json",
        },
        use_cache=False,
    )

    assert (status, text) == (200, "ok")
    redirected = {k.lower(): v for k, v in calls[1][2]["headers"].items()}
    assert redirected == {"accept": "application/json"}
    assert calls[0][2]["allow_redirects"] is False
    assert calls[1][2]["allow_redirects"] is False


def test_same_origin_redirect_keeps_authorization_but_rebuilds_host(
    isolated_http, monkeypatch,
):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((url, kwargs["headers"]))
        if len(calls) == 1:
            return _Response(302, headers={"Location": "/next"})
        return _Response(200, "ok")

    monkeypatch.setattr(http._session, "request", fake_request)
    status, _ = http.fetch(
        "https://api.example/start",
        headers={"Host": "api.example", "Authorization": "Bearer intended"},
        use_cache=False,
    )

    assert status == 200
    assert calls[1] == (
        "https://api.example/next", {"Authorization": "Bearer intended"}
    )


def test_https_redirect_cannot_downgrade_to_cleartext(isolated_http, monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append(url)
        return _Response(302, headers={"Location": "http://api.example/plain"})

    monkeypatch.setattr(http._session, "request", fake_request)
    with pytest.raises(http.UnsafeExternalURL, match="downgrade"):
        http.fetch("https://api.example/start", use_cache=False)
    assert calls == ["https://api.example/start"]


def test_cross_origin_307_cannot_forward_json_body(isolated_http, monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((url, kwargs.get("json")))
        return _Response(307, headers={"Location": "https://other.example/write"})

    monkeypatch.setattr(http._session, "request", fake_request)
    with pytest.raises(http.UnsafeExternalURL, match="request body"):
        http.fetch(
            "https://api.example/write", method="POST",
            json_body={"token": "do-not-forward"}, use_cache=False,
        )
    assert calls == [("https://api.example/write", {"token": "do-not-forward"})]


def test_external_text_neutralizes_html_and_markdown_structure():
    hostile = (
        "\u202e## trusted\n<script title=\"x\">alert(1)</script>\n"
        "![tracking](javascript:alert(1))\n```html\n<img src=x>\n```"
    )

    rendered = sanitize_external(hostile)

    assert "\n" not in rendered
    assert "\u202e" not in rendered
    assert "<script" not in rendered and "<img" not in rendered
    assert 'title="x"' not in rendered
    assert "![" not in rendered and "](" not in rendered
    assert "```" not in rendered
    assert "&lt;script title=&quot;x&quot;&gt;" in rendered
    assert "javascript:alert(1)" in rendered  # visible evidence, not an active link


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "https://user:password@example.com/job",
        "https://example.com/a\nInjected: yes",
        "https://example.com/job\n",
        "https://example.com\\@127.0.0.1/job",
        "http://localhost/admin",
        "http://sub.localhost/admin",
        "http://printer.local/admin",
        "http://service.internal/admin",
        "http://router/admin",
        "http://router.home/admin",
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://[::7f00:1]/admin",
        "http://[64:ff9b::7f00:1]/admin",
        "http://[fec0::1]/admin",
        "http://[ff02::1]/admin",
        "http://224.0.0.1/admin",
        "http://2130706433/admin",
        "http://0177.0.0.1/admin",
        "http://0x7f000001/admin",
        "http://127。0。0。1/admin",
    ],
)
def test_external_url_renderer_rejects_active_or_local_targets(url):
    assert sanitize_external_url(url) == ""


def test_external_url_renderer_encodes_markdown_delimiters():
    safe = sanitize_external_url(
        "HTTPS://Example.COM/jobs/a_(1)*[draft]?next=(two)[x]#frag"
    )
    assert safe.startswith("https://example.com/")
    assert "(" not in safe and ")" not in safe
    assert "[" not in safe and "]" not in safe
    assert "*" not in safe and "`" not in safe
    assert "%28" in safe and "%29" in safe and "%5B" in safe


@pytest.mark.parametrize("address", ["::7f00:1", "64:ff9b::7f00:1"])
def test_fetch_ip_policy_rejects_ipv6_encodings_of_loopback(address):
    with pytest.raises(http.UnsafeExternalURL, match="non-public"):
        http._public_ip(address)


@pytest.mark.parametrize("address", ["fec0::1", "ff02::1", "224.0.0.1"])
def test_fetch_ip_policy_rejects_site_local_and_multicast(address):
    with pytest.raises(http.UnsafeExternalURL, match="non-public"):
        http._public_ip(address)


def test_linkedin_guest_uses_the_shared_truthful_user_agent(monkeypatch):
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return 403, ""

    monkeypatch.setattr(aggregators, "TERMS", ("program manager",))
    monkeypatch.setattr(aggregators, "fetch", fake_fetch)

    assert aggregators.linkedin_guest({"pages": 1}) == []
    assert len(calls) == 1
    assert "headers" not in calls[0][1]
    assert "CareerKit" in http.UA and "Chrome" not in http.UA


@pytest.mark.parametrize(
    ("version", "safe"),
    [
        ("0.13.1", False),
        ("0.14.0", False),
        ("0.14.1rc1", False),
        ("0.14.1", True),
        ("0.14.1.post1", True),
        ("1.1.0", True),
        ("not-a-version", False),
    ],
)
def test_markdownify_security_version_gate(version, safe):
    assert aggregators._safe_markdownify_version(version) is safe


def test_jobspy_fails_closed_before_importing_vulnerable_markdownify(monkeypatch):
    def installed_version(distribution):
        return {"python-jobspy": "1.1.82", "markdownify": "0.13.1"}[distribution]

    monkeypatch.setattr(aggregators.metadata, "version", installed_version)
    with pytest.raises(RuntimeError, match=r"CVE-2025-46656.*markdownify>=0\.14\.1"):
        aggregators.jobspy_feed({})


def test_jobspy_rejects_secure_but_dependency_incompatible_markdownify(monkeypatch):
    monkeypatch.setattr(
        aggregators.metadata, "version",
        lambda name: {"python-jobspy": "1.1.82", "markdownify": "1.1.0"}[name],
    )
    distribution = type("Distribution", (), {
        "requires": ["markdownify>=0.13.1,<0.14.0"],
    })()
    monkeypatch.setattr(aggregators.metadata, "distribution",
                        lambda _name: distribution)
    with pytest.raises(RuntimeError, match="does not declare compatibility"):
        aggregators._require_safe_jobspy_runtime()


def test_jobspy_allows_a_future_declared_compatible_secure_graph(monkeypatch):
    monkeypatch.setattr(
        aggregators.metadata, "version",
        lambda name: {"python-jobspy": "2.0.0", "markdownify": "1.1.0"}[name],
    )
    distribution = type("Distribution", (), {
        "requires": ["markdownify>=0.14.1"],
    })()
    monkeypatch.setattr(aggregators.metadata, "distribution",
                        lambda _name: distribution)
    aggregators._require_safe_jobspy_runtime()


def test_blank_company_jobs_with_different_urls_have_distinct_identity():
    first = Job(
        company="", title="Program Manager", url="https://feed.example/jobs/1",
        source="jobspy:indeed", external_id="one",
    )
    second = Job(
        company=" ", title="Program Manager", url="https://feed.example/jobs/2",
        source="jobspy:indeed", external_id="two",
    )

    assert first.uid != second.uid
    assert first.group_key != second.group_key


def test_blank_company_same_url_resighting_is_stable():
    first = Job(
        company="", title="Program Manager",
        url="HTTPS://Feed.Example:443/jobs/1#first-card",
        source="JobSpy:Indeed", external_id="changing-aggregator-id",
    )
    again = Job(
        company="", title="Program Manager",
        url="https://feed.example/jobs/1#second-card",
        source="jobspy:indeed", external_id="new-id",
    )

    assert first.uid == again.uid
    assert first.group_key == again.group_key
    assert first.legacy_blank_company_uid != first.uid
    assert first.legacy_blank_company_group_key != first.group_key


def test_blank_company_source_is_part_of_the_disambiguator():
    kwargs = {
        "company": "", "title": "Program Manager",
        "url": "https://employer.example/jobs/1",
    }
    indeed = Job(source="jobspy:indeed", **kwargs)
    linkedin = Job(source="linkedin_guest", **kwargs)

    assert indeed.uid != linkedin.uid
    assert indeed.group_key != linkedin.group_key


def test_named_company_identity_still_collapses_aggregator_urls():
    first = Job(
        company="Acme Inc.", title="Program Manager",
        url="https://feed.example/jobs/1", source="jobspy:indeed",
    )
    second = Job(
        company="ACME", title="Program Manager",
        url="https://other.example/jobs/99", source="linkedin_guest",
    )

    assert first.uid == second.uid
    assert first.group_key == second.group_key
    assert first.legacy_blank_company_uid == ""
    assert first.legacy_blank_company_group_key == ""


def test_blank_company_ats_identity_still_uses_requisition_id():
    first = Job(
        company="", title="Program Manager",
        url="https://boards.example/jobs/first", source="greenhouse",
        external_id="req-7",
    )
    again = Job(
        company="", title="Program Manager",
        url="https://boards.example/jobs/moved", source="greenhouse",
        external_id="req-7",
    )

    assert first.uid == again.uid
    assert first.group_key == again.group_key
    assert first.legacy_blank_company_uid == ""
    assert first.legacy_blank_company_group_key == ""
