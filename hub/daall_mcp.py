"""User-triggered, read-only lookups against Mnaqasat's *public MCP* service.

No crawling, bulk export, scheduled pull or persistence of this provider's data.
This is an interactive search proxy; the third-party site remains the authoritative
publisher of its own index, and the original Etimad page remains the official record.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx

MCP_URL = "https://mnaqasat.daall.sa/mcp"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-03-26",
    "User-Agent": "GHAZI-Business-Hub/1.0 (user-initiated public MCP lookup)",
}
ALLOWED_TOOLS = frozenset({"get_tender", "search_tenders", "company_summary", "open_tenders"})


class ProviderError(Exception):
    """Provider/network/format error intended to be surfaced without sensitive details."""


def safe_source_url(url: str | None) -> str:
    """Only public Daall/Etimad HTTPS detail links are allowed in displayed results."""
    if not isinstance(url, str) or len(url) > 1000:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return url if parts.scheme == "https" and parts.hostname in {
        "mnaqasat.daall.sa", "tenders.etimad.sa", "portal.etimad.sa",
    } and not parts.username and not parts.password else ""


def _decode(response: httpx.Response) -> dict:
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("content-type", ""):
        items = []
        for line in response.text.splitlines():
            if line.startswith("data: "):
                try:
                    items.append(json.loads(line[6:]))
                except (ValueError, TypeError):
                    pass
        return next((x for x in items if isinstance(x, dict) and ("result" in x or "error" in x)), {})
    result = response.json()
    return result if isinstance(result, dict) else {}


def lookup(tool: str, **arguments) -> dict | list | str:
    """Perform exactly one tool call for one visitor request. No automatic pagination."""
    if tool not in ALLOWED_TOOLS:
        raise ValueError("Unsupported MCP action")
    if any(len(v) > 120 for v in arguments.values() if isinstance(v, str)):
        raise ValueError("Invalid search length")
    with httpx.Client(timeout=httpx.Timeout(11.0), follow_redirects=False) as client:
        try:
            init = _decode(client.post(MCP_URL, headers=MCP_HEADERS, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "GhaziBusinessHub", "version": "1.0"}},
            }))
            if init.get("error") or not init.get("result"):
                raise ProviderError("تعذّر تهيئة الاتصال بمصدر المناقصات الخارجي.")
            headers = dict(MCP_HEADERS)
            session_id = init.get("result", {}).get("sessionId")
            # The MCP session id comes from the response header, never from the user.
            session_id = client.cookies.get("mcp-session-id") or session_id
            if session_id and isinstance(session_id, str) and len(session_id) < 200:
                headers["Mcp-Session-Id"] = session_id
            client.post(MCP_URL, headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            result = _decode(client.post(MCP_URL, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }))
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
            raise ProviderError("تعذّر الاتصال بمصدر المناقصات. يُرجى المحاولة لاحقًا.") from exc
    if result.get("error") or result.get("result", {}).get("isError"):
        raise ProviderError("لم يتمكن مصدر المناقصات من إكمال الاستعلام.")
    contents = result.get("result", {}).get("content", [])
    strings = [c.get("text", "") for c in contents if isinstance(c, dict) and c.get("type") == "text"]
    body = "\n".join(strings).strip()
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError:
        return body[:10000]


def tender_link(reference: str) -> str:
    """Provider detail URL by official numeric reference, never synthetic."""
    return f"https://mnaqasat.daall.sa/tender/{reference}" if reference.isdecimal() and len(reference) <= 30 else ""
