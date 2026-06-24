"""Web tools: search and page fetch (stdlib HTML extraction, SSRF guarded)."""

from __future__ import annotations

import html as html_mod
import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from .registry import ToolContext, tool

MAX_FETCH_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 30.0
UA = "Mozilla/5.0 (compatible; sophagent/0.1)"


def _assert_public_host(url: str) -> None:
    """Reject URLs whose host resolves to private/loopback ranges (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"cannot resolve host: {host!r}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"host {host!r} resolves to a non-public address")


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def extract_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parser.parts))


@tool(
    "web_fetch",
    "Fetch a web page and return its text content.",
    {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
)
async def web_fetch(ctx: ToolContext, url: str) -> str:
    try:
        _assert_public_host(url)
    except ValueError as e:
        return f"Error: {e}"
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True, headers={"User-Agent": UA}) as client:
        try:
            resp = await client.get(url)
        except httpx.HTTPError as e:
            return f"Error: fetch failed: {e}"
    if len(resp.content) > MAX_FETCH_BYTES:
        return f"Error: response too large (> {MAX_FETCH_BYTES} bytes)"
    ctype = resp.headers.get("content-type", "")
    if "html" in ctype:
        return extract_text(resp.text)
    return resp.text


# --- search ----------------------------------------------------------------

_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'(?:<a[^>]+class="result__snippet"[^>]*>(.*?)</a>)?',
    re.S,
)
_TAG = re.compile(r"<[^>]+>")


async def _ddg_search(query: str, count: int) -> str:
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, headers={"User-Agent": UA}) as client:
        resp = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
    results = []
    for m in _DDG_RESULT.finditer(resp.text):
        url, title, snippet = m.group(1), _TAG.sub("", m.group(2)), _TAG.sub("", m.group(3) or "")
        results.append(f"- {html_mod.unescape(title).strip()}\n  {url}\n  {html_mod.unescape(snippet).strip()}")
        if len(results) >= count:
            break
    return "\n".join(results) if results else "No results."


async def _tavily_search(query: str, count: int, api_key: str) -> str:
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": api_key, "query": query, "max_results": count},
        )
        resp.raise_for_status()
    results = resp.json().get("results", [])
    return "\n".join(f"- {r['title']}\n  {r['url']}\n  {r.get('content', '')[:300]}" for r in results) or "No results."


@tool(
    "web_search",
    "Search the web. Returns a list of titles, URLs and snippets.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "count": {"type": "integer", "default": 5, "description": "Max results (1-10)"},
        },
        "required": ["query"],
    },
)
async def web_search(ctx: ToolContext, query: str, count: int = 5) -> str:
    count = max(1, min(count, 10))
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    try:
        if tavily_key:
            return await _tavily_search(query, count, tavily_key)
        return await _ddg_search(query, count)
    except httpx.HTTPError as e:
        return f"Error: search failed: {e}"
