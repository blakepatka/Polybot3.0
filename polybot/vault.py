"""Credential vault.

Keys are validated locally, written to the gitignored ``.env`` beside this
project, and never echoed back to the browser in plaintext. Everything the API
returns about a stored credential is masked — the dashboard only ever learns
*that* a value is present and the last few characters of it.

Validation is deliberately done before the write, so a typo'd key is rejected
rather than silently persisted and then failing at order time.
"""

from __future__ import annotations

import os
import re
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import ENV_PATH

_LOCK = threading.RLock()

# Every secret this project stores. Anything not in this list is left untouched
# when rewriting .env, so hand-added variables survive a vault save.
SECRET_KEYS = (
    "POLY_PRIVATE_KEY",
    "POLY_WALLET_ADDRESS",
    "POLY_FUNDER_ADDRESS",
    "POLY_SIGNATURE_TYPE",
    # Set only after the saved L2 credentials authenticate against CLOB V2.
    # Legacy V1 credentials are deliberately not treated as live-ready.
    "POLY_CLOB_VERSION",
    "POLY_API_KEY",
    "POLY_API_SECRET",
    "POLY_API_PASSPHRASE",
    # Chainlink Data Streams — the venue's own resolution source. Read-only
    # market data; it cannot move funds, unlike the keys above.
    "CHAINLINK_API_KEY",
    "CHAINLINK_API_SECRET",
)

_HEX64 = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class VaultError(ValueError):
    """Raised when a credential fails validation. Message is user-facing.

    Never include the offending secret in the message — these surface in the UI.
    """


@dataclass
class VaultStatus:
    loaded: bool
    address: str | None
    funder: str | None
    signature_type: int
    has_private_key: bool
    has_api_creds: bool
    clob_v2_verified: bool
    masked: dict[str, str]
    live_enabled: bool
    has_chainlink: bool = False


def mask(value: str | None, keep: int = 4) -> str:
    """Render a secret safe to display: length-preserving-ish, tail-revealing."""
    if not value:
        return ""
    value = value.strip()
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * min(len(value) - keep, 12) + value[-keep:]


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a .env into a dict. Tolerates comments, blanks, and quoting."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    """Rewrite .env preserving unknown keys, then tighten file permissions."""
    existing = _read_env_file(path)
    existing.update(values)

    lines = [
        "# Written by the Polybot credential vault. Gitignored — do not commit.",
        "# Values here are read at engine start; restart the engine after editing.",
        "",
    ]
    for key in SECRET_KEYS:
        lines.append(f"{key}={existing.get(key, '')}")

    extra = [k for k in sorted(existing) if k not in SECRET_KEYS]
    if extra:
        lines += ["", "# Other settings"]
        lines += [f"{k}={existing[k]}" for k in extra]

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Best-effort: strip group/other access. No-op semantics differ on Windows,
    # where NTFS ACLs govern instead, so failure here is not fatal.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def normalize_private_key(raw: str) -> str:
    """Return a 0x-prefixed 32-byte hex key, or raise VaultError."""
    key = (raw or "").strip()
    if not key:
        raise VaultError("Private key is required.")
    if not _HEX64.match(key):
        raise VaultError("Private key must be 64 hex characters (with or without a 0x prefix).")
    if not key.startswith("0x"):
        key = "0x" + key
    if int(key, 16) == 0:
        raise VaultError("Private key is all zeros — that is the example placeholder, not a real key.")
    return key.lower()


def derive_address(private_key: str) -> str:
    """Derive the EOA address for a key. Raises VaultError if unusable."""
    try:
        from eth_account import Account
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise VaultError("eth-account is not installed; run pip install -r requirements.txt") from exc
    try:
        return Account.from_key(private_key).address
    except Exception as exc:
        raise VaultError(f"Could not derive an address from that private key: {exc}") from exc


def validate_bundle(
    private_key: str,
    wallet_address: str = "",
    api_key: str = "",
    api_secret: str = "",
    api_passphrase: str = "",
    funder_address: str = "",
    signature_type: int = 0,
) -> dict[str, Any]:
    """Validate a full credential bundle without persisting anything.

    Checks that are cheap and local run first; the network round-trip to the
    CLOB only happens once the key itself is known to be well-formed.
    """
    key = normalize_private_key(private_key)
    derived = derive_address(key)

    wallet_address = (wallet_address or "").strip()
    if wallet_address:
        if not _ADDR.match(wallet_address):
            raise VaultError("Wallet address must be a 0x-prefixed 40-hex-character address.")
        if wallet_address.lower() != derived.lower():
            raise VaultError(
                f"Private key derives {derived}, which does not match the wallet address you entered. "
                "Clear the wallet address field to accept the derived one."
            )

    funder_address = (funder_address or "").strip()
    if funder_address and not _ADDR.match(funder_address):
        raise VaultError("Funder address must be a 0x-prefixed 40-hex-character address.")

    if signature_type not in (0, 1, 2, 3):
        raise VaultError(
            "Signature type must be 0 (EOA), 1 (email proxy), "
            "2 (legacy Safe), or 3 (V2 deposit wallet)."
        )

    # L2 credentials are optional here: if absent we can derive them from the
    # key at connect time. If present, they must be well-formed.
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()
    api_passphrase = (api_passphrase or "").strip()
    partial = [bool(api_key), bool(api_secret), bool(api_passphrase)]
    if any(partial) and not all(partial):
        raise VaultError("CLOB L2 credentials are all-or-nothing — supply key, secret, and passphrase together.")
    if api_key and not _UUID.match(api_key):
        raise VaultError("CLOB L2 API key should be a UUID.")

    return {
        "private_key": key,
        "address": derived,
        "funder_address": funder_address or derived,
        "signature_type": signature_type,
        "api_key": api_key,
        "api_secret": api_secret,
        "api_passphrase": api_passphrase,
    }


def check_clob_credentials(bundle: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    """Ask CLOB V2 whether these credentials actually authenticate.

    Returns a report rather than raising, because a network hiccup should not
    look like a bad key. Only ``ok=False`` with a definite ``reason`` means the
    credentials were rejected.
    """
    report: dict[str, Any] = {"ok": False, "checked": False, "reason": None, "derived_api_key": None}
    try:
        from py_clob_client_v2 import ApiCreds, ClobClient
    except ImportError:
        report["reason"] = "py-clob-client-v2 is not installed; skipped online check."
        return report

    try:
        api_creds = None
        if bundle["api_key"]:
            api_creds = ApiCreds(
                api_key=bundle["api_key"],
                api_secret=bundle["api_secret"],
                api_passphrase=bundle["api_passphrase"],
            )

        client = ClobClient(
            host="https://clob.polymarket.com",
            key=bundle["private_key"],
            chain_id=137,
            creds=api_creds,
            signature_type=bundle["signature_type"],
            funder=bundle["funder_address"],
        )
        if api_creds is None:
            creds = client.create_or_derive_api_key()
            bundle["api_key"] = creds.api_key
            bundle["api_secret"] = creds.api_secret
            bundle["api_passphrase"] = creds.api_passphrase
            client.set_api_creds(creds)
            report["derived_api_key"] = creds.api_key

        # A cheap authenticated read. Success means the L2 headers signed fine.
        client.get_api_keys()
        report.update(ok=True, checked=True)
    except Exception as exc:
        report.update(checked=True, reason=str(exc)[:300])
    return report


def save(bundle: dict[str, Any]) -> None:
    """Persist a validated bundle to .env and to this process's environment."""
    with _LOCK:
        values = {
            "POLY_PRIVATE_KEY": bundle["private_key"],
            "POLY_WALLET_ADDRESS": bundle["address"],
            "POLY_FUNDER_ADDRESS": bundle["funder_address"],
            "POLY_SIGNATURE_TYPE": str(bundle["signature_type"]),
            "POLY_CLOB_VERSION": str(bundle.get("clob_version", "")),
            "POLY_API_KEY": bundle.get("api_key", ""),
            "POLY_API_SECRET": bundle.get("api_secret", ""),
            "POLY_API_PASSPHRASE": bundle.get("api_passphrase", ""),
        }
        _write_env_file(ENV_PATH, values)
        os.environ.update(values)


def clear() -> None:
    """Wipe stored credentials from .env and the live environment."""
    with _LOCK:
        _write_env_file(ENV_PATH, {k: "" for k in SECRET_KEYS})
        for key in SECRET_KEYS:
            os.environ.pop(key, None)


def load_into_env() -> None:
    """Hydrate os.environ from .env without clobbering real env vars."""
    for key, value in _read_env_file(ENV_PATH).items():
        os.environ.setdefault(key, value)


def current() -> dict[str, str]:
    """Read the stored bundle from the environment (plaintext, internal use)."""
    return {k: os.getenv(k, "") for k in SECRET_KEYS}


def refresh_clob_credentials() -> dict[str, Any]:
    """Derive and persist a fresh CLOB V2 L2 credential set.

    This is intentionally called only by an explicit dashboard action. It
    reuses the stored signer/funder details without returning any secret.
    """
    values = current()
    if not values.get("POLY_PRIVATE_KEY"):
        raise VaultError("No private key is stored. Enter the wallet details first.")
    try:
        signature_type = int(values.get("POLY_SIGNATURE_TYPE") or 0)
    except ValueError as exc:
        raise VaultError("The stored signature type is invalid.") from exc

    bundle = validate_bundle(
        private_key=values["POLY_PRIVATE_KEY"],
        wallet_address=values.get("POLY_WALLET_ADDRESS", ""),
        funder_address=values.get("POLY_FUNDER_ADDRESS", ""),
        signature_type=signature_type,
    )
    report = check_clob_credentials(bundle)
    if not report.get("ok"):
        raise VaultError(
            "Polymarket did not accept the V2 credentials: "
            + str(report.get("reason") or "unknown error")
        )
    bundle["clob_version"] = "2"
    save(bundle)
    return {
        "ok": True,
        "address": bundle["address"],
        "funder": bundle["funder_address"],
        "derived_api_key": report.get("derived_api_key"),
    }


def save_chainlink(api_key: str, api_secret: str) -> dict[str, Any]:
    """Validate and store Data Streams credentials.

    Kept separate from the wallet bundle: these are read-only market-data keys
    with no ability to move funds, and requiring a private key just to add a
    price feed would be nonsense.
    """
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()
    if not api_key or not api_secret:
        raise VaultError("Both the Data Streams API key and secret are required.")
    if len(api_key) < 8 or len(api_secret) < 8:
        raise VaultError("That does not look like a Data Streams key/secret pair.")
    api_key = api_key.lower()
    with _LOCK:
        values = {"CHAINLINK_API_KEY": api_key, "CHAINLINK_API_SECRET": api_secret}
        _write_env_file(ENV_PATH, values)
        os.environ.update(values)
    return {"ok": True, "masked_key": mask(api_key, keep=6)}


def clear_chainlink() -> None:
    with _LOCK:
        _write_env_file(ENV_PATH, {"CHAINLINK_API_KEY": "", "CHAINLINK_API_SECRET": ""})
        for key in ("CHAINLINK_API_KEY", "CHAINLINK_API_SECRET"):
            os.environ.pop(key, None)


def status() -> VaultStatus:
    """Masked, browser-safe view of what the vault holds."""
    from .settings import live_trading_enabled

    values = current()
    pk = values["POLY_PRIVATE_KEY"]
    has_api = all(values[k] for k in ("POLY_API_KEY", "POLY_API_SECRET", "POLY_API_PASSPHRASE"))
    clob_v2_verified = values.get("POLY_CLOB_VERSION") == "2"
    has_chainlink = bool(values["CHAINLINK_API_KEY"] and values["CHAINLINK_API_SECRET"])
    try:
        sig_type = int(values["POLY_SIGNATURE_TYPE"] or 0)
    except ValueError:
        sig_type = 0

    return VaultStatus(
        loaded=bool(pk),
        address=values["POLY_WALLET_ADDRESS"] or None,
        funder=values["POLY_FUNDER_ADDRESS"] or None,
        signature_type=sig_type,
        has_private_key=bool(pk),
        has_api_creds=has_api,
        clob_v2_verified=clob_v2_verified,
        masked={
            "private_key": mask(pk),
            "api_key": mask(values["POLY_API_KEY"], keep=6),
            "api_secret": mask(values["POLY_API_SECRET"]),
            "api_passphrase": mask(values["POLY_API_PASSPHRASE"]),
            "chainlink_key": mask(values["CHAINLINK_API_KEY"], keep=6),
            "chainlink_secret": mask(values["CHAINLINK_API_SECRET"]),
        },
        live_enabled=live_trading_enabled(),
        has_chainlink=has_chainlink,
    )
