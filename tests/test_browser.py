"""Browser tool: SSRF policy, redirect handling, content extraction, and the
allow_browser gate in the loop.

All tests run offline: fetch_url takes injected fetcher/resolver fakes, so no
socket is ever opened. Self-contained by repo convention — the scripted model
source and runtime helper are copied, not imported across test modules.
"""

from __future__ import annotations

import json

import pytest

from agentconnect.common.schemas import GenerateRequest, GenerateResponse, TaskSubmission
from agentconnect.runtime import LangGraphAgentRuntime, RuntimeConfig, parse_action
from agentconnect.runtime.tools.browser import FetchResponse, fetch_url


class ScriptedModelSource:
    """Replays a fixed sequence of model replies; repeats the last one."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.requests: list[GenerateRequest] = []

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        self.requests.append(req)
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return GenerateResponse(request_id=req.request_id, model_id=req.model_id, output_text=text)


class FakeFetcher:
    """Scripted responses keyed by URL; records every fetched URL. A `default`
    response answers URLs not in the table (used for redirect loops)."""

    def __init__(self, responses: dict[str, FetchResponse] | None = None,
                 default: FetchResponse | None = None):
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float, max_bytes: int) -> FetchResponse:
        self.calls.append(url)
        if url in self.responses:
            return self.responses[url]
        assert self.default is not None, f"unexpected fetch: {url}"
        return self.default


class FakeResolver:
    """Maps hostnames to address lists; unknown hosts raise OSError like
    getaddrinfo. Records every resolved hostname."""

    def __init__(self, table: dict[str, list[str]]):
        self.table = dict(table)
        self.calls: list[str] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        if host not in self.table:
            raise OSError(f"unknown host: {host}")
        return list(self.table[host])


PUBLIC_IP = "93.184.216.34"  # example.com


def _resp(status: int = 200, body: bytes = b"", content_type: str | None = "text/plain",
          location: str | None = None) -> FetchResponse:
    headers = {}
    if content_type is not None:
        headers["content-type"] = content_type
    if location is not None:
        headers["location"] = location
    return FetchResponse(status=status, headers=headers, body=body)


def _fetch(url: str, fetcher: FakeFetcher, resolver: FakeResolver, **kw) -> str:
    return fetch_url(url, fetcher=fetcher, resolver=resolver, **kw)


# ------------------------------------------------------------ scheme + SSRF
@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/pub/x", "gopher://example.com/1"]
)
def test_non_http_scheme_rejected_before_any_fetch(url):
    fetcher = FakeFetcher()
    resolver = FakeResolver({"example.com": [PUBLIC_IP]})
    obs = _fetch(url, fetcher, resolver)
    assert obs.startswith("ERROR: URL scheme must be http or https")
    assert fetcher.calls == []


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",            # loopback
        "10.0.0.5",             # RFC1918
        "192.168.1.1",          # RFC1918
        "169.254.169.254",      # link-local / cloud metadata
        "[::1]",                # IPv6 loopback
        "[::ffff:127.0.0.1]",   # IPv4-mapped loopback
        "100.64.0.1",           # CGNAT
    ],
)
def test_blocked_ip_literals(host):
    fetcher = FakeFetcher()
    obs = _fetch(f"http://{host}/", fetcher, FakeResolver({}))
    assert "blocked address" in obs
    assert fetcher.calls == []


def test_private_resolving_host_blocked():
    fetcher = FakeFetcher()
    resolver = FakeResolver({"internal.corp": ["10.1.2.3"]})
    obs = _fetch("http://internal.corp/secrets", fetcher, resolver)
    assert "blocked address" in obs
    assert fetcher.calls == []


def test_mixed_public_and_private_resolution_fails_closed():
    fetcher = FakeFetcher()
    resolver = FakeResolver({"evil.example": [PUBLIC_IP, "127.0.0.1"]})
    obs = _fetch("http://evil.example/", fetcher, resolver)
    assert "blocked address" in obs
    assert fetcher.calls == []


def test_unresolvable_host():
    obs = _fetch("http://nosuch.invalid/", FakeFetcher(), FakeResolver({}))
    assert "could not resolve host: nosuch.invalid" in obs


def test_userinfo_rejected():
    fetcher = FakeFetcher()
    resolver = FakeResolver({"example.com": [PUBLIC_IP]})
    obs = _fetch("http://user:pass@example.com/", fetcher, resolver)
    assert "credentials in URLs are not allowed" in obs
    assert fetcher.calls == [] and resolver.calls == []


# ------------------------------------------------------------------ content
def test_html_reduced_to_text():
    html = (
        b"<html><head><title>Docs Home</title>"
        b"<script>var secret = 'payload';</script>"
        b"<style>body { color: red }</style></head>"
        b"<body><h1>Welcome</h1><p>Read the <a href='/guide'>guide</a>.</p></body></html>"
    )
    fetcher = FakeFetcher({"http://example.com/docs": _resp(body=html, content_type="text/html; charset=utf-8")})
    obs = _fetch("http://example.com/docs", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert obs.startswith("OK: fetched http://example.com/docs (status 200, text/html)")
    body = obs.split("\n", 1)[1]
    assert body.splitlines()[0] == "Docs Home"
    assert "Welcome" in body and "Read the guide." in body
    assert "<" not in body            # no tags survive
    assert "payload" not in body      # script content dropped


def test_text_plain_passthrough():
    fetcher = FakeFetcher({"http://example.com/notes.txt": _resp(body=b"line one\nline two\n")})
    obs = _fetch("http://example.com/notes.txt", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert "OK: fetched http://example.com/notes.txt (status 200, text/plain)" in obs
    assert "line one\nline two" in obs


def test_json_passthrough():
    fetcher = FakeFetcher(
        {"http://example.com/api": _resp(body=b'{"ok": true}', content_type="application/json")}
    )
    obs = _fetch("http://example.com/api", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert '{"ok": true}' in obs and "application/json" in obs


def test_no_content_type_html_body_is_sniffed_and_extracted():
    """Finding 9: a 200 with no Content-Type whose body looks like HTML is
    sniffed and reduced to text (tags dropped)."""
    body = b"<!doctype html><html><head><title>Sniffed</title></head><body><p>Hello there</p></body></html>"
    fetcher = FakeFetcher({"http://example.com/page": _resp(body=body, content_type=None)})
    obs = _fetch("http://example.com/page", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert obs.startswith("OK: fetched http://example.com/page (status 200, unknown)")
    text = obs.split("\n", 1)[1]
    assert text.splitlines()[0] == "Sniffed"
    assert "Hello there" in text
    assert "<" not in text


def test_no_content_type_plain_body_passthrough():
    """Finding 9: a 200 with no Content-Type whose body is not HTML passes
    through verbatim (no tag stripping/mangling)."""
    fetcher = FakeFetcher({"http://example.com/raw": _resp(body=b"just plain text", content_type=None)})
    obs = _fetch("http://example.com/raw", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert obs.startswith("OK: fetched http://example.com/raw (status 200, unknown)")
    assert "just plain text" in obs


# ---------------------------------------------------------------- redirects
def test_redirect_followed_and_revalidated():
    fetcher = FakeFetcher(
        {
            "http://a.example/start": _resp(302, location="http://b.example/final", content_type=None),
            "http://b.example/final": _resp(body=b"made it"),
        }
    )
    resolver = FakeResolver({"a.example": [PUBLIC_IP], "b.example": ["8.8.8.8"]})
    obs = _fetch("http://a.example/start", fetcher, resolver)
    assert obs.startswith("OK: fetched http://b.example/final")
    assert "made it" in obs
    # both hops were resolved and both were fetched, in order
    assert resolver.calls == ["a.example", "b.example"]
    assert fetcher.calls == ["http://a.example/start", "http://b.example/final"]


def test_redirect_to_metadata_ip_blocked():
    fetcher = FakeFetcher(
        {"http://a.example/": _resp(302, location="http://169.254.169.254/latest/meta-data/")}
    )
    obs = _fetch("http://a.example/", fetcher, FakeResolver({"a.example": [PUBLIC_IP]}))
    assert "blocked address" in obs
    assert fetcher.calls == ["http://a.example/"]


def test_redirect_to_file_scheme_blocked():
    fetcher = FakeFetcher({"http://a.example/": _resp(301, location="file:///etc/passwd")})
    obs = _fetch("http://a.example/", fetcher, FakeResolver({"a.example": [PUBLIC_IP]}))
    assert "URL scheme must be http or https" in obs
    assert fetcher.calls == ["http://a.example/"]


def test_relative_location_joined():
    fetcher = FakeFetcher(
        {
            "http://a.example/dir/page": _resp(301, location="../other"),
            "http://a.example/other": _resp(body=b"joined"),
        }
    )
    obs = _fetch("http://a.example/dir/page", fetcher, FakeResolver({"a.example": [PUBLIC_IP]}))
    assert obs.startswith("OK: fetched http://a.example/other")
    assert fetcher.calls[1] == "http://a.example/other"


def test_redirect_without_location_header():
    """Finding 5: a 3xx hop carrying no Location header yields a specific ERROR
    and no further fetch is attempted."""
    fetcher = FakeFetcher({"http://a.example/": _resp(302, location=None)})
    obs = _fetch("http://a.example/", fetcher, FakeResolver({"a.example": [PUBLIC_IP]}))
    assert obs == "ERROR: redirect without a Location header: http://a.example/"
    assert fetcher.calls == ["http://a.example/"]


def test_redirect_loop_capped():
    fetcher = FakeFetcher(default=_resp(302, location="http://a.example/loop"))
    obs = _fetch("http://a.example/loop", fetcher, FakeResolver({"a.example": [PUBLIC_IP]}),
                 max_redirects=5)
    assert obs == "ERROR: too many redirects (limit 5): http://a.example/loop"
    assert len(fetcher.calls) == 6  # exactly max_redirects + 1 fetches


# --------------------------------------------------------- caps and failures
def test_over_cap_body_carries_truncation_marker():
    body = b"x" * 101  # fetcher hands back max_bytes + 1
    fetcher = FakeFetcher({"http://example.com/big": _resp(body=body)})
    obs = _fetch("http://example.com/big", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}),
                 max_bytes=100)
    assert "[response truncated at 100 bytes]" in obs
    assert "x" * 101 not in obs


def test_http_error_status():
    fetcher = FakeFetcher({"http://example.com/gone": _resp(404, body=b"nope")})
    obs = _fetch("http://example.com/gone", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert obs == "ERROR: HTTP status 404 for http://example.com/gone"


def test_binary_content_type_unsupported():
    fetcher = FakeFetcher(
        {"http://example.com/logo.png": _resp(body=b"\x89PNG", content_type="image/png")}
    )
    obs = _fetch("http://example.com/logo.png", fetcher, FakeResolver({"example.com": [PUBLIC_IP]}))
    assert obs == "ERROR: unsupported content type 'image/png' for http://example.com/logo.png"


def test_fetch_timeout_message():
    class TimingOutFetcher:
        def __call__(self, url, timeout, max_bytes):
            raise TimeoutError("timed out")

    obs = fetch_url(
        "http://example.com/slow",
        fetcher=TimingOutFetcher(),
        resolver=FakeResolver({"example.com": [PUBLIC_IP]}),
    )
    assert obs == "ERROR: fetch timed out after 20s: http://example.com/slow"


def test_generic_fetch_failure_becomes_error(monkeypatch):
    """Finding 6: connection-refused / DNS-at-connect / TLS errors surface as a
    plain OSError or URLError. They must become an ERROR observation, not an
    exception escaping the loop."""
    import urllib.error

    for exc in (OSError("connection refused"), urllib.error.URLError("tls handshake failed")):
        class _Boom:
            def __call__(self, url, timeout, max_bytes):
                raise exc

        obs = fetch_url(
            "http://example.com/x",
            fetcher=_Boom(),
            resolver=FakeResolver({"example.com": [PUBLIC_IP]}),
        )
        assert obs.startswith("ERROR: fetch failed:")


def test_incomplete_read_mid_body_becomes_error():
    """Finding 1: a chunked response truncated mid-body raises IncompleteRead, a
    subclass of http.client.HTTPException that is NOT an OSError. It must be
    caught and converted to an ERROR observation (never-exceptions contract)."""
    import http.client

    class _Truncated:
        def __call__(self, url, timeout, max_bytes):
            raise http.client.IncompleteRead(partial=b"half", expected=100)

    obs = fetch_url(
        "http://example.com/chunked",
        fetcher=_Truncated(),
        resolver=FakeResolver({"example.com": [PUBLIC_IP]}),
    )
    assert obs.startswith("ERROR: fetch failed:")


def test_read_capped_enforces_wall_clock_deadline(monkeypatch):
    """Finding 2: the default reader must bound total read time, not per-recv
    time. A response that keeps returning bytes (each recv inside the socket
    timeout) is aborted with TimeoutError once the wall clock passes the
    deadline, instead of pinning the thread until the byte cap is reached."""
    from agentconnect.runtime.tools import browser

    clock = {"now": 0.0}
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock["now"])

    class _Trickle:
        """Returns one byte per read1 and advances the fake clock by 1s each
        call — a slow-trickle body that never itself times out."""

        def read1(self, n):
            clock["now"] += 1.0
            return b"x"

    with pytest.raises(TimeoutError):
        # deadline = 3.0; limit is huge so only the deadline can stop it.
        browser._read_capped(_Trickle(), limit=1_000_000, deadline=3.0)


def test_read_capped_stops_at_limit_and_eof():
    """The reader returns exactly the available bytes (up to the limit) when the
    body is short, and honours the byte cap when it is long."""
    from agentconnect.runtime.tools import browser

    class _Fixed:
        def __init__(self, data):
            self.data = data

        def read1(self, n):
            chunk, self.data = self.data[:n], self.data[n:]
            return chunk

    far = browser.time.monotonic() + 3600
    assert browser._read_capped(_Fixed(b"hello"), limit=100, deadline=far) == b"hello"
    assert browser._read_capped(_Fixed(b"x" * 500), limit=10, deadline=far) == b"x" * 10


# ------------------------------------------------------------ action parsing
def test_parse_fetch_url_action():
    a = parse_action('{"action": "fetch_url", "url": "https://example.com/docs"}')
    assert a.kind == "fetch_url" and a.args["url"] == "https://example.com/docs"
    assert parse_action('{"action": "fetch_url"}').kind == "invalid"
    assert parse_action('{"action": "fetch_url", "url": ""}').kind == "invalid"


# ------------------------------------------------------------------ the loop
def _finish(summary: str, **kw) -> str:
    return json.dumps({"action": "finish", "summary": summary, "confidence": 0.9, **kw})


_FETCH_DOCS = json.dumps({"action": "fetch_url", "url": "http://example.com/docs"})


def _runtime(replies, tmp_path, *, fetcher=None, url_resolver=None, **cfg):
    source = ScriptedModelSource(replies)
    config = RuntimeConfig(workspace_root=str(tmp_path), **cfg)
    rt = LangGraphAgentRuntime(source, config, fetcher=fetcher, url_resolver=url_resolver)
    return rt, source


def test_browser_disabled_by_default(tmp_path):
    fetcher = FakeFetcher()
    rt, source = _runtime([_FETCH_DOCS, _finish("gave up")], tmp_path, fetcher=fetcher)
    result = rt.run(TaskSubmission(task="fetch the docs"), task_id="b1")
    assert result.status == "completed"
    obs = source.requests[1].messages[-1]["content"]
    assert "the browser action is disabled" in obs
    assert fetcher.calls == []
    assert result.evidence_refs == []


def test_browser_enabled_end_to_end(tmp_path):
    html = b"<html><head><title>Docs</title></head><body><p>install with pip</p></body></html>"
    fetcher = FakeFetcher({"http://example.com/docs": _resp(body=html, content_type="text/html")})
    resolver = FakeResolver({"example.com": [PUBLIC_IP]})
    rt, source = _runtime(
        [_FETCH_DOCS, _finish("read the docs")],
        tmp_path,
        fetcher=fetcher,
        url_resolver=resolver,
        allow_browser=True,
    )
    result = rt.run(TaskSubmission(task="fetch the docs"), task_id="b2")
    assert result.status == "completed"
    assert "fetch_url:http://example.com/docs" in result.evidence_refs
    obs = source.requests[1].messages[-1]["content"]
    assert obs.startswith("OBSERVATION:\nOK: fetched http://example.com/docs")
    assert "install with pip" in obs


@pytest.mark.parametrize("allow_browser", [False, True])
def test_prompt_line_conditional_on_browser_gate(tmp_path, allow_browser):
    rt, source = _runtime([_finish("done")], tmp_path, allow_browser=allow_browser)
    rt.run(TaskSubmission(task="anything"), task_id="b3")
    prompt = source.requests[0].messages[0]["content"]
    assert ('"action": "fetch_url"' in prompt) is allow_browser


def test_blocked_fetch_is_not_evidence(tmp_path):
    fetcher = FakeFetcher()
    rt, source = _runtime(
        [json.dumps({"action": "fetch_url", "url": "http://127.0.0.1/admin"}), _finish("blocked")],
        tmp_path,
        fetcher=fetcher,
        allow_browser=True,
    )
    result = rt.run(TaskSubmission(task="poke loopback"), task_id="b4")
    obs = source.requests[1].messages[-1]["content"]
    assert "blocked address" in obs
    assert result.evidence_refs == []
    assert fetcher.calls == []
