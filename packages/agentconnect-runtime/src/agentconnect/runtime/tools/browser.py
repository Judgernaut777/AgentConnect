"""Browser tool: fetch one http(s) URL and return it as readable text.

Gated by ``RuntimeConfig.allow_browser`` (default False — network egress),
enforced in the graph like the shell gate. Requests are GET-only: no cookies,
no auth, no userinfo in URLs, no environment proxies (a proxy would tunnel
past the IP check), default TLS verification.

SSRF policy: redirects are followed manually (urllib must never follow them
itself, or hops would escape the check) and EVERY hop's host must resolve
exclusively to globally routable addresses — ``ipaddress.is_global`` after
unwrapping IPv4-mapped IPv6 — which fails closed on any loopback, RFC1918,
link-local (incl. 169.254.169.254), CGNAT, ULA, unspecified, or reserved
address in the answer. The residual DNS-rebinding TOCTOU between our
resolve-check and urllib's own connect is accepted for this worker runtime:
pinning the vetted IP while keeping TLS SNI/hostname verification needs a
custom HTTPSConnection, out of scope; the per-hop re-check plus the
fail-closed multi-address policy is the contracted hardening level.

Bodies are read to at most ``max_bytes + 1`` and marked when capped; each hop
gets its own timeout, so the worst case is ``(max_redirects + 1) * timeout``.
All failures come back as ``ERROR: ...`` observation strings, never
exceptions.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Callable, NamedTuple
from urllib.parse import urljoin, urlsplit


class FetchResponse(NamedTuple):
    status: int
    headers: dict[str, str]  # keys lowercased; 'content-type', 'location' matter
    body: bytes  # fetcher reads at most max_bytes + 1


Fetcher = Callable[[str, float, int], FetchResponse]  # (url, timeout, max_bytes)
Resolver = Callable[[str, int], "list[str]"]  # (host, port) -> IPs; raises OSError

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# Content types rendered as-is (after decoding); HTML types get text extraction.
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_TEXT_TYPES = frozenset({"application/json", "application/xml"})


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface 3xx as HTTPError instead of following it: every hop must pass
    the scheme + address checks before it is fetched."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Read the body one recv at a time so the wall-clock deadline is enforced
# between recvs (see _read_capped); 64 KiB balances syscalls against latency.
_READ_CHUNK = 65536


def _read_capped(resp, limit: int, deadline: float) -> bytes:
    """Read up to ``limit`` bytes but abort with TimeoutError once ``deadline``
    (a time.monotonic() value) passes. urlopen's ``timeout`` only bounds a
    single socket operation, so a slow-trickle server that dribbles bytes just
    under that timeout would pin the thread for hours on one read() call. We
    read via read1 (at most one recv per call, returning whatever is available)
    and check the wall clock between calls, making the documented per-hop bound
    real instead of per-recv."""
    # 200 responses are HTTPResponse (has read1); error-status responses arrive
    # as an addinfourl wrapper whose underlying .fp is the HTTPResponse. Prefer
    # read1 (one recv per call) so the deadline check between calls is real; the
    # plain read() fallback exists only for exotic response objects.
    read1 = (
        getattr(resp, "read1", None)
        or getattr(getattr(resp, "fp", None), "read1", None)
        or resp.read
    )
    chunks: list[bytes] = []
    remaining = limit
    while remaining > 0:
        if time.monotonic() >= deadline:
            raise TimeoutError("read deadline exceeded")
        chunk = read1(min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _urlopen_fetch(url: str, timeout: float, max_bytes: int) -> FetchResponse:
    opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "agentconnect-runtime/0.2",
            "Accept": "text/html, text/*;q=0.9, */*;q=0.5",
        },
    )
    deadline = time.monotonic() + timeout
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # With the no-redirect handler, HTTPError IS the response for 3xx/4xx/5xx.
        resp = e
    with resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return FetchResponse(resp.status, headers, _read_capped(resp, max_bytes + 1, deadline))


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def _check_host(host: str, port: int, resolve: Resolver) -> str | None:
    """Return an ERROR observation unless every address the host stands for is
    globally routable. Fail-closed: one blocked address rejects the request."""
    try:
        addresses = [host]
        ipaddress.ip_address(host)  # IP literal — check it directly
    except ValueError:
        try:
            addresses = resolve(host, port)
        except OSError:
            return f"ERROR: could not resolve host: {host}"
        if not addresses:
            return f"ERROR: could not resolve host: {host}"
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return f"ERROR: URL host resolves to a blocked address: {host}"
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped  # blocks e.g. ::ffff:127.0.0.1
        if not ip.is_global:
            return f"ERROR: URL host resolves to a blocked address: {host}"
    return None


def fetch_url(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = 1_000_000,
    max_redirects: int = 5,
    fetcher: Fetcher | None = None,
    resolver: Resolver | None = None,
) -> str:
    """Fetch ``url`` (GET) and return a readable-text observation string."""
    fetch = fetcher or _urlopen_fetch
    resolve = resolver or _default_resolver
    current = url
    resp: FetchResponse | None = None
    for _hop in range(max_redirects + 1):
        try:
            parts = urlsplit(current)
            host = parts.hostname
            port = parts.port
        except ValueError:
            return f"ERROR: invalid URL: {current!r}"
        if parts.scheme not in ("http", "https"):
            return f"ERROR: URL scheme must be http or https: {parts.scheme!r}"
        if not host:
            return f"ERROR: invalid URL: {current!r}"
        if parts.username is not None or parts.password is not None:
            return f"ERROR: credentials in URLs are not allowed: {current!r}"
        err = _check_host(host, port or (443 if parts.scheme == "https" else 80), resolve)
        if err is not None:
            return err
        try:
            resp = fetch(current, timeout, max_bytes)
        except (TimeoutError, socket.timeout):
            return f"ERROR: fetch timed out after {timeout:.0f}s: {current}"
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            # http.client.HTTPException (e.g. IncompleteRead on a mid-body
            # connection drop) is NOT an OSError, so without it a truncated
            # chunked response would escape this loop as an unhandled exception,
            # breaking the "failures come back as ERROR strings" contract.
            return f"ERROR: fetch failed: {exc}"
        if resp.status in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return f"ERROR: redirect without a Location header: {current}"
            current = urljoin(current, location)  # next hop re-runs every check
            continue
        if not 200 <= resp.status < 300:
            return f"ERROR: HTTP status {resp.status} for {current}"
        break
    else:
        return f"ERROR: too many redirects (limit {max_redirects}): {url}"
    return _render(current, resp, max_bytes)


def _render(final_url: str, resp: FetchResponse, max_bytes: int) -> str:
    body = resp.body
    truncated = len(body) > max_bytes
    body = body[:max_bytes]
    ct_header = resp.headers.get("content-type", "")
    mime = ct_header.split(";", 1)[0].strip().lower()
    charset = "utf-8"
    for param in ct_header.split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset" and value.strip():
            charset = value.strip().strip("\"'")

    def _decode() -> str:
        try:
            return body.decode(charset, errors="replace")
        except LookupError:
            return body.decode("utf-8", errors="replace")

    if mime in _HTML_TYPES:
        text = _html_to_text(_decode())
    elif not mime:
        text = _decode()
        head = text[:256].lower()
        if "<html" in head or "<!doctype" in head:
            text = _html_to_text(text)
    elif mime.startswith("text/") or mime in _TEXT_TYPES or mime.endswith(("+json", "+xml")):
        text = _decode()
    else:
        # Never feed binary to the model.
        return f"ERROR: unsupported content type {mime!r} for {final_url}"
    if truncated:
        # Distinct from the graph's char-level marker: a short extraction from
        # a capped body must still be known-partial.
        text += f"\n[response truncated at {max_bytes} bytes]"
    return f"OK: fetched {final_url} (status {resp.status}, {mime or 'unknown'})\n{text}"


# Elements whose content is never prose. They nest (inline svg can hold more
# svg), hence a depth counter rather than a flag.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
# Elements that break the text flow: emit a newline on open and close.
_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "hr", "li", "ul", "ol", "dl", "dt", "dd",
        "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "main", "nav", "aside",
        "blockquote", "pre", "form",
    }
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._title: list[str] = []
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")
        elif tag in ("td", "th"):
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self._title.append(data)
        else:
            self._parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self._title).split())

    @property
    def text(self) -> str:
        lines = []
        for line in "".join(self._parts).split("\n"):
            collapsed = " ".join(line.split())
            if collapsed:
                lines.append(collapsed)
        return "\n".join(lines)


def _html_to_text(html: str) -> str:
    """Reduce HTML to readable text: title first, block structure as newlines,
    anchor text kept (hrefs dropped), script/style/svg payloads removed.
    HTMLParser tolerates truncated markup, so a byte-capped body parses fine."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    title, text = parser.title, parser.text
    if title and text:
        return f"{title}\n{text}"
    return title or text
