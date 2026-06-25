from __future__ import annotations

"""
Thin wrapper around python-keyring for macOS Keychain access.
All app secrets are stored under service name 'robinhood-trader'.

Only called from local host processes — never from k8s pods (which receive
secrets via SealedSecret → k8s Secret → env var).
"""

import keyring
import keyring.errors

SERVICE = "robinhood-trader"


def get(key: str) -> str | None:
    return keyring.get_password(SERVICE, key)


def set(key: str, value: str) -> None:
    keyring.set_password(SERVICE, key, value)


def delete(key: str) -> None:
    try:
        keyring.delete_password(SERVICE, key)
    except keyring.errors.PasswordDeleteError:
        pass


def require(key: str) -> str:
    value = get(key)
    if not value:
        raise RuntimeError(
            f"Secret '{key}' not found in Keychain (service={SERVICE}). "
            f"Run:  uv run inv keychain-set {key} <value>"
        )
    return value
