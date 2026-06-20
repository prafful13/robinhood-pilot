from __future__ import annotations
"""
OAuth 2.0 PKCE flow for Robinhood MCP authentication.

Token storage is environment-aware:
  - Local host:  macOS Keychain (via vault.keychain), key = 'oauth_tokens'
  - k8s pod:     file at TOKEN_FILE env var (mounted from k8s Secret)
                 KUBERNETES_SERVICE_HOST is set automatically by k8s in every pod.

On first run (local), opens a browser window and listens on localhost:3118 for the callback.
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from aiohttp import web

KEYCHAIN_KEY = "oauth_tokens"
_CLIENT_ID_KEYCHAIN_KEY = "client_id"


def _get_client_id(cfg: dict) -> str:
    if cid := cfg.get("client_id"):
        return cid
    if cid := os.environ.get("ROBINHOOD_CLIENT_ID"):
        return cid
    if not _is_in_cluster():
        try:
            from vault.keychain import get
            if cid := get(_CLIENT_ID_KEYCHAIN_KEY):
                return cid
        except Exception:
            pass
    raise ValueError(
        "ROBINHOOD_CLIENT_ID not found.\n"
        "  Local: uv run inv keychain-set client_id <value>\n"
        "  k8s:   uv run inv k8s-seal && kubectl apply -f k8s/sealed/"
    )


def _is_in_cluster() -> bool:
    return "KUBERNETES_SERVICE_HOST" in os.environ


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


async def _run_callback_server(expected_state: str) -> tuple[str, str]:
    result: dict = {}
    ready = asyncio.Event()

    async def handle(request: web.Request) -> web.Response:
        result["code"] = request.rel_url.query.get("code", "")
        result["state"] = request.rel_url.query.get("state", "")
        ready.set()
        return web.Response(
            text="<h2>Authentication successful! You can close this tab.</h2>",
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/callback", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "localhost", 3118)
    await site.start()
    try:
        await asyncio.wait_for(ready.wait(), timeout=120)
    finally:
        await runner.cleanup()

    if result.get("state") != expected_state:
        raise ValueError("OAuth state mismatch — possible CSRF attack")
    return result["code"], result["state"]


async def _discover_token_url(mcp_url: str, fallback: str) -> str:
    origin = mcp_url.rstrip("/").rsplit("/", 1)[0] if mcp_url.count("/") > 2 else mcp_url
    for base in (origin, f"https://{urlparse(mcp_url).netloc}"):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{base}/.well-known/oauth-authorization-server")
                if r.status_code == 200:
                    return r.json()["token_endpoint"]
        except Exception:
            pass
    return fallback


async def _exchange_code(
    token_url: str,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def _refresh_access_token(token_url: str, client_id: str, refresh_tok: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_tok,
            },
        )
        resp.raise_for_status()
        return resp.json()


def _is_expired(tokens: dict, buffer_secs: int = 300) -> bool:
    saved_at = tokens.get("saved_at", 0)
    expires_in = tokens.get("expires_in", 0)
    return time.time() > saved_at + expires_in - buffer_secs


def _load_tokens() -> dict | None:
    if _is_in_cluster():
        token_file = os.environ.get("TOKEN_FILE", "/secrets/rh_tokens.json")
        path = Path(token_file)
        if path.exists():
            return json.loads(path.read_text())
        return None
    else:
        from vault.keychain import get
        value = get(KEYCHAIN_KEY)
        return json.loads(value) if value else None


def _save_tokens(tokens: dict) -> None:
    tokens["saved_at"] = time.time()
    if _is_in_cluster():
        token_file = os.environ.get("TOKEN_FILE", "/secrets/rh_tokens.json")
        Path(token_file).write_text(json.dumps(tokens, indent=2))
    else:
        from vault.keychain import set as kc_set
        kc_set(KEYCHAIN_KEY, json.dumps(tokens))


async def get_access_token(cfg: dict) -> str:
    """
    Returns a valid access token, running the full PKCE browser flow if needed.
    Locally: tokens stored in macOS Keychain.
    In k8s: tokens read from TOKEN_FILE (mounted from k8s Secret).
    """
    tokens = _load_tokens()

    if tokens and not _is_expired(tokens):
        return tokens["access_token"]

    token_url = await _discover_token_url(cfg["resource"], cfg["token_url"])

    if tokens and tokens.get("refresh_token") and _is_expired(tokens):
        try:
            fresh = await _refresh_access_token(token_url, _get_client_id(cfg), tokens["refresh_token"])
            _save_tokens(fresh)
            return fresh["access_token"]
        except Exception:
            pass  # fall through to full auth

    # Full PKCE flow (local only — pods never reach here)
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type": "code",
        "client_id": _get_client_id(cfg),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
        "scope": cfg["scope"],
        "resource": cfg["resource"],
    }
    auth_url = cfg["auth_url"] + "?" + urlencode(params)

    print(f"\nOpening browser for Robinhood authentication...\n{auth_url}\n")
    webbrowser.open(auth_url)

    code, _ = await _run_callback_server(state)
    tokens = await _exchange_code(
        token_url, _get_client_id(cfg), cfg["redirect_uri"], code, code_verifier
    )
    _save_tokens(tokens)
    print("Authentication successful. Token saved to macOS Keychain.")
    return tokens["access_token"]
