from __future__ import annotations
"""
MCP client that wraps Robinhood's agent HTTP endpoint.
Each public method corresponds to a Robinhood MCP tool.
"""

import json
import logging
import time
import uuid
from datetime import datetime, timedelta

import httpx
import pytz

from broker.oauth import get_access_token

log = logging.getLogger(__name__)

# Exceptions that indicate a stale TCP connection (e.g. after laptop sleep)
_STALE_CONNECTION_ERRORS = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)


MCP_URL = "https://agent.robinhood.com/mcp/trading"
ET = pytz.timezone("America/New_York")


class RobinhoodClient:
    def __init__(self, cfg: dict):
        self._cfg = cfg["broker"]
        self._token: str | None = None
        self._token_loaded_at: float = 0
        self._session_id: str | None = None
        self._call_id = 0
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._http = httpx.AsyncClient(timeout=30)
        await self._ensure_token()
        await self._initialize_session()
        return self

    async def __aexit__(self, *_):
        if self._http:
            await self._http.aclose()

    async def _ensure_token(self):
        # Reload token max once per 5 min to avoid repeated file reads
        if self._token and time.time() - self._token_loaded_at < 300:
            return
        self._token = await get_access_token(self._cfg)
        self._token_loaded_at = time.time()

    def get_token_data(self) -> dict | None:
        """Return raw token dict (saved_at, expires_in) for dashboard status display."""
        from broker.oauth import _load_tokens
        return _load_tokens()

    def _next_id(self) -> int:
        self._call_id += 1
        return self._call_id

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _reconnect(self) -> None:
        """Rebuild the HTTP client and re-initialize the MCP session.
        Called after a stale connection error (e.g. laptop woke from sleep).
        """
        log.warning("MCP connection lost — reconnecting...")
        try:
            await self._http.aclose()
        except Exception:
            pass
        self._http = httpx.AsyncClient(timeout=30)
        self._session_id = None
        self._token = None  # force token reload too
        await self._ensure_token()
        await self._initialize_session()
        log.info("MCP reconnected")

    async def _post(self, payload: dict) -> dict:
        await self._ensure_token()
        try:
            resp = await self._http.post(MCP_URL, json=payload, headers=self._headers())
        except _STALE_CONNECTION_ERRORS:
            # Laptop woke from sleep — TCP connections are dead. Reconnect and retry once.
            await self._reconnect()
            resp = await self._http.post(MCP_URL, json=payload, headers=self._headers())

        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session_id = sid

        if resp.status_code == 401:
            # Token expired mid-session; force re-auth and retry once
            self._token = None
            await self._ensure_token()
            resp = await self._http.post(MCP_URL, json=payload, headers=self._headers())

        resp.raise_for_status()

        ct = resp.headers.get("content-type", "")
        if "text/event-stream" in ct:
            return self._parse_sse(resp.text)
        return resp.json()

    def _parse_sse(self, text: str) -> dict:
        for line in text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    return json.loads(data)
        return {}

    async def _initialize_session(self):
        payload = {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "robinhood-trader", "version": "1.0.0"},
            },
            "id": self._next_id(),
        }
        await self._post(payload)
        # Send initialized notification (fire-and-forget, no response expected)
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        try:
            await self._http.post(MCP_URL, json=notif, headers=self._headers())
        except Exception:
            pass

    async def call_tool(self, name: str, arguments: dict) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": self._next_id(),
        }
        resp = await self._post(payload)
        raw = resp.get("result", {}).get("content", [{}])[0].get("text", "{}")
        return json.loads(raw)

    # ── Public API ──────────────────────────────────────────────────────────

    async def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        result = await self.call_tool("get_equity_quotes", {"symbols": symbols})
        quotes = result.get("data", {}).get("quotes", [])
        return {q["symbol"]: q for q in quotes}

    async def get_historicals(self, symbols: list[str], interval: str = "15minute", lookback_days: int = 7) -> dict[str, list]:
        start = (datetime.now(pytz.UTC) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = await self.call_tool("get_equity_historicals", {
            "symbols": symbols,
            "start_time": start,
            "interval": interval,
            "bounds": "regular",
        })
        out = {}
        for item in result.get("data", {}).get("results", []):
            out[item["symbol"]] = item.get("historicals", [])
        return out

    async def get_positions(self, account_number: str) -> list[dict]:
        result = await self.call_tool("get_equity_positions", {"account_number": account_number})
        return result.get("data", {}).get("positions", [])

    async def get_portfolio(self, account_number: str) -> dict:
        result = await self.call_tool("get_portfolio", {"account_number": account_number})
        return result.get("data", {})

    async def review_order(self, account_number: str, symbol: str, side: str, dollar_amount: str) -> dict:
        return await self.call_tool("review_equity_order", {
            "account_number": account_number,
            "symbol": symbol,
            "side": side,
            "type": "market",
            "dollar_amount": dollar_amount,
            "time_in_force": "gfd",
        })

    async def review_sell_order(self, account_number: str, symbol: str, quantity: str) -> dict:
        return await self.call_tool("review_equity_order", {
            "account_number": account_number,
            "symbol": symbol,
            "side": "sell",
            "type": "market",
            "quantity": quantity,
            "time_in_force": "gfd",
        })

    async def place_buy_order(self, account_number: str, symbol: str, dollar_amount: str) -> dict:
        return await self.call_tool("place_equity_order", {
            "account_number": account_number,
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "dollar_amount": dollar_amount,
            "time_in_force": "gfd",
            "ref_id": str(uuid.uuid4()),
        })

    async def place_sell_order(self, account_number: str, symbol: str, quantity: str) -> dict:
        return await self.call_tool("place_equity_order", {
            "account_number": account_number,
            "symbol": symbol,
            "side": "sell",
            "type": "market",
            "quantity": quantity,
            "time_in_force": "gfd",
            "ref_id": str(uuid.uuid4()),
        })

    async def get_orders(self, account_number: str) -> list[dict]:
        result = await self.call_tool("get_equity_orders", {"account_number": account_number})
        return result.get("data", {}).get("orders", [])
