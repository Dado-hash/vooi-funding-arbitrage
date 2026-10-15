"""Client minimale per il server MCP VOOI (Streamable HTTP, JSON-RPC 2.0).

Espone gli stessi tool che Claude vede come `mcp__vooi-perps__*`, chiamati per nome
(`get_funding_strategies`, `get_markets`, ...).
"""

import itertools
import json
import os
import threading

import httpx

from .config import load_dotenv

DEFAULT_URL = "https://perps-api.vooi.io/mcp"
PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class VooiClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 30.0):
        load_dotenv()
        self.url = url or os.environ.get("VOOI_MCP_URL") or DEFAULT_URL
        token = token or os.environ.get("VOOI_MCP_TOKEN")
        if not token:
            raise MCPError("VOOI_MCP_TOKEN mancante: copia .env.example in .env (vedi README)")
        header = os.environ.get("VOOI_MCP_AUTH_HEADER") or "Authorization"
        scheme = os.environ.get("VOOI_MCP_AUTH_SCHEME", "Bearer")
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                header: f"{scheme} {token}" if scheme else token,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._init_lock = threading.Lock()
        self._session_id: str | None = None
        self._initialized = False

    def _next_id(self) -> int:
        with self._lock:
            return next(self._ids)

    def _post(self, payload: dict):
        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        r = self._http.post(self.url, json=payload, headers=headers)
        if r.status_code >= 400:
            raise MCPError(f"HTTP {r.status_code}: {r.text[:300]}")
        if sid := r.headers.get("mcp-session-id"):
            self._session_id = sid
        if "id" not in payload:
            return None
        return self._parse(r, payload["id"])

    @staticmethod
    def _parse(r: httpx.Response, req_id: int):
        if "text/event-stream" in r.headers.get("content-type", ""):
            messages = []
            for event in r.text.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in event.splitlines() if l.startswith("data:"))
                if data:
                    messages.append(json.loads(data))
        else:
            body = r.json()
            messages = body if isinstance(body, list) else [body]
        for m in messages:
            if m.get("id") == req_id:
                if "error" in m:
                    raise MCPError(f"JSON-RPC error: {m['error']}")
                return m["result"]
        raise MCPError("risposta MCP senza id corrispondente")

    def _ensure_session(self) -> None:
        # Lock dedicato: i worker paralleli aspettano che l'handshake sia finito.
        with self._init_lock:
            if self._initialized:
                return
            self._post({
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "vooi-arb", "version": "0.1.0"},
                },
            })
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._initialized = True

    def call(self, tool: str, args: dict | None = None):
        """Chiama un tool e restituisce il JSON decodificato (o il testo grezzo)."""
        self._ensure_session()
        result = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool, "arguments": args or {}},
        })
        text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
        if result.get("isError"):
            raise MCPError(f"{tool}: {text}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def paginate(self, tool: str, args: dict, page_size: int = 100, max_items: int = 5000, stop=None) -> list:
        """Scorre `{items, hasMore}`. `stop(item)` sull'ultimo elemento di pagina interrompe."""
        items: list = []
        offset = 0
        while True:
            page = self.call(tool, {**args, "limit": page_size, "offset": offset})
            batch = page.get("items", [])
            items.extend(batch)
            if not page.get("hasMore") or len(items) >= max_items:
                break
            if stop and batch and stop(batch[-1]):
                break
            offset += page_size
        return items
