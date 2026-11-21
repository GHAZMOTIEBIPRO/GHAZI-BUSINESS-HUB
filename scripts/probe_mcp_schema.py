"""Non-harvesting handshake probe of Daall public read-only MCP tool schemas.

No tender or company data queried or stored. Intended only for compatibility testing.
"""
import json

import httpx

URL = "https://mnaqasat.daall.sa/mcp"
BASE_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                "MCP-Protocol-Version": "2025-03-26", "User-Agent": "GhaziBusinessHubMCP/0.1"}


def parse(response):
    response.raise_for_status()
    typ = response.headers.get("content-type", "")
    if "text/event-stream" in typ:
        events = []
        for line in response.text.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except ValueError:
                    pass
        return next((x for x in events if "result" in x), events[0] if events else {})
    return response.json()


def main():
    with httpx.Client(timeout=12, follow_redirects=False) as c:
        i = c.post(URL, headers=BASE_HEADERS,
                   json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                       "protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "GhaziBusinessHub-compat-check", "version": "0.1"}}})
        init = parse(i)
        print("init_ok", "result" in init, "status", i.status_code, "session", bool(i.headers.get("mcp-session-id")))
        h = dict(BASE_HEADERS)
        if i.headers.get("mcp-session-id"):
            h["Mcp-Session-Id"] = i.headers["mcp-session-id"]
        c.post(URL, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        t = c.post(URL, headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        out = parse(t)
        tools = out.get("result", {}).get("tools", [])
        print("tools", json.dumps([{"name": x["name"], "schema": x.get("inputSchema", {})}
                                   for x in tools], ensure_ascii=False))
        data = c.post(URL, headers=h, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                          "params": {"name": "get_tender", "arguments": {"reference_no": "260439001976"}}})
        result = parse(data)
        content = result.get("result", {}).get("content", [])
        first_text = next((x.get("text", "") for x in content if x.get("type") == "text"), "")
        try:
            parsed = json.loads(first_text)
            print("tender_result_shape", json.dumps({"root_type": type(parsed).__name__,
                "keys": list(parsed)[:60] if isinstance(parsed, dict) else [],
                "item_type": type(parsed[0]).__name__ if isinstance(parsed, list) and parsed else "n/a",
                "content_size": len(first_text)}, ensure_ascii=False))
        except ValueError:
            print("tender_response_shape", json.dumps({"content_length": len(first_text),
                "excerpt": first_text[:180].replace("\\n", " ")}, ensure_ascii=False))



if __name__ == "__main__":
    main()
