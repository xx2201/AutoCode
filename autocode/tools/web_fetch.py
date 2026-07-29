"""Safe public-web retrieval with redirect visibility and bounded output."""

from __future__ import annotations

import ipaddress
import socket
import time
from html.parser import HTMLParser
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .base import ConcurrencySpec, Tool

_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_OUTPUT_CHARS = 50_000
_CACHE_SECONDS = 15 * 60
_PROXY_SYNTHETIC_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_cache: dict[str, tuple[float, str]] = {}
_cache_lock = Lock()


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch a public HTTP(S) page and return readable text for the supplied extraction prompt. "
        "HTTP is upgraded to HTTPS; private-network targets are blocked; redirects are reported."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Public HTTP(S) URL"},
            "prompt": {"type": "string", "description": "What information to extract from the page"},
        },
        "required": ["url", "prompt"],
    }

    def concurrency_spec(self, arguments: dict) -> ConcurrencySpec:
        return ConcurrencySpec.parallel("independent public web request")

    def execute(self, url: str, prompt: str) -> str:
        try:
            normalized = _normalize_url(url)
            _validate_public_host(normalized)
            with _cache_lock:
                cached = _cache.get(normalized)
            if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
                return _format_result(normalized, prompt, cached[1], cached=True)

            opener = build_opener(_NoRedirect())
            request = Request(
                normalized,
                headers={"User-Agent": "AutoCoder/0.1 web_fetch", "Accept": "text/html,text/plain,application/json"},
            )
            try:
                response = opener.open(request, timeout=20)
            except HTTPError as exc:
                if 300 <= exc.code < 400 and exc.headers.get("Location"):
                    target = urljoin(normalized, exc.headers["Location"])
                    return (
                        f"Redirect detected ({exc.code}) to {target}. "
                        "Call web_fetch again with the redirected URL."
                    )
                return f"Error: HTTP {exc.code} while fetching {normalized}"
            content_type = response.headers.get_content_type()
            if not (
                content_type.startswith("text/")
                or content_type in {"application/json", "application/xml", "application/xhtml+xml"}
            ):
                return f"Error: unsupported response content type {content_type}"
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            truncated = len(raw) > _MAX_RESPONSE_BYTES
            raw = raw[:_MAX_RESPONSE_BYTES]
            charset = response.headers.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="replace")
            if "html" in content_type:
                parser = _ReadableHTML()
                parser.feed(text)
                text = parser.text()
            if truncated:
                text += "\n\n[Response truncated at 2 MB.]"
            with _cache_lock:
                _cache[normalized] = (time.monotonic(), text)
            return _format_result(normalized, prompt, text, cached=False)
        except (ValueError, OSError, URLError) as exc:
            return f"Error: {exc}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _ReadableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._hidden = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._hidden += 1
        elif not self._hidden and tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._hidden:
            self._hidden -= 1
        elif not self._hidden and tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self._parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self._parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self._parts).splitlines())
        return "\n".join(line for line in lines if line)


def _normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"}:
        raise ValueError("web_fetch only supports HTTP(S) URLs")
    if parts.username or parts.password:
        raise ValueError("URLs containing credentials are not allowed")
    if not parts.hostname:
        raise ValueError("URL must include a hostname")
    scheme = "https" if parts.scheme == "http" else parts.scheme
    return urlunsplit((scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _validate_public_host(url: str) -> None:
    host = urlsplit(url).hostname
    assert host is not None
    if host.lower() in {"localhost", "metadata.google.internal"}:
        raise ValueError("private or metadata network targets are blocked")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("private or non-public network targets are blocked")
    for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(result[4][0])
        if not address.is_global and address not in _PROXY_SYNTHETIC_NETWORK:
            raise ValueError("private or non-public network targets are blocked")


def _format_result(url: str, prompt: str, content: str, *, cached: bool) -> str:
    body = content[:_MAX_OUTPUT_CHARS]
    suffix = "\n\n[Content truncated at 50,000 characters.]" if len(content) > len(body) else ""
    cache_note = " (cached)" if cached else ""
    return f"Source: {url}{cache_note}\nExtraction request: {prompt}\n\n{body}{suffix}"
