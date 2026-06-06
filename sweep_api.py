"""
sweep_api.py — Sweep Haus API client, per-chain index caching, and collection refresh.

Index files are keyed by chain_key (human-readable string from rpc.json):
  data/sweep_index_x1_testnet.json
  data/sweep_index_base_mainnet.json

Mint history is stored per-wallet:
  data/minted_0xabcd1234.json   (one file per wallet, keyed by address[:10])

This eliminates the global minted-state lock bottleneck: each wallet file has its own
lock, so 50 concurrent wallets don't serialize on a single file write.

Bearer token rotation: round-robin per API call.
401 handling: immediately raises BearerTokenError — caller skips the chain, no stale data.

[RISK] The Sweep Haus API is undocumented/private. Auth scheme changes will break this.
[RISK] Per-wallet files still need in-process locking — locks dict is created on demand.
       Do not run two bot instances against the same data/ directory simultaneously.
"""

import json
import logging
import os
import random
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger("sweep_api")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
SWEEP_API_URL = "https://api.sweep.haus/api/nft-collections"

_BEARER_TOKENS: list[str] = []
_bearer_index = 0
_bearer_lock = threading.Lock()

# Per-chain index locks (created on demand, keyed by chain_key)
_index_locks: dict[str, threading.Lock] = {}
_index_locks_meta = threading.Lock()

# Per-wallet minted-state locks (created on demand, keyed by address.lower())
_wallet_locks: dict[str, threading.Lock] = {}
_wallet_locks_meta = threading.Lock()


class BearerTokenError(Exception):
    """Raised when the API returns 401 — token expired or invalid."""
    pass


# =====================================================================
# BEARER TOKEN MANAGEMENT
# =====================================================================

def load_bearer_tokens() -> None:
    """
    Load bearer tokens from environment variables.
    Supports: BEARER (single) or BEARER_1, BEARER_2, ... (multiple).
    """
    global _BEARER_TOKENS
    tokens = []

    i = 1
    while True:
        val = os.environ.get(f"BEARER_{i}", "").strip()
        if not val:
            break
        tokens.append(val)
        i += 1

    if not tokens:
        single = os.environ.get("BEARER", "").strip()
        if single:
            tokens.append(single)

    if not tokens:
        raise EnvironmentError(
            "[sweep_api] No bearer tokens found in environment.\n"
            "Set BEARER=... or BEARER_1=..., BEARER_2=... in your .env file."
        )

    _BEARER_TOKENS = tokens
    logger.info(f"[sweep_api] Loaded {len(tokens)} bearer token(s).")


def _next_bearer() -> str:
    global _bearer_index
    with _bearer_lock:
        token = _BEARER_TOKENS[_bearer_index % len(_BEARER_TOKENS)]
        _bearer_index += 1
    return token


# Realistic UA pool — rotated per request so all API calls don't share a fingerprint.
# Mix of Chrome/Firefox/Safari/Edge across Windows/Mac/Linux.
# Versions as of June 2026: Chrome 149, Firefox 151, Safari 26, Edge 149.
# UPDATE THIS LIST every ~2 months as browsers release new major versions.
_USER_AGENTS = [
    # Chrome 149 — current stable (June 2, 2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    # Chrome 148 — previous stable (still in wide circulation)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    # Firefox 151 — current stable (May 19, 2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.5; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
    # Firefox 150 — previous stable (still in wide circulation)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    # Safari 26 — current stable (May 11, 2026), macOS Tahoe
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_5) AppleWebKit/621.1.15 (KHTML, like Gecko) Version/26.0 Safari/621.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/621.1.15 (KHTML, like Gecko) Version/26.0 Safari/621.1.15",
    # Edge 149 — current stable (Chromium-based)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0",
]


def _make_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sweep.haus/",
        "Origin": "https://sweep.haus",
        "Authorization": f"Bearer {_next_bearer()}",
    }


# =====================================================================
# INDEX FILE HELPERS (per-chain)
# =====================================================================

def _index_path(chain_key: str) -> str:
    return os.path.join(DATA_DIR, f"sweep_index_{chain_key}.json")


def _get_index_lock(chain_key: str) -> threading.Lock:
    with _index_locks_meta:
        if chain_key not in _index_locks:
            _index_locks[chain_key] = threading.Lock()
        return _index_locks[chain_key]


def load_index(chain_key: str) -> dict:
    path = _index_path(chain_key)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[sweep_api] Corrupt index '{path}': {e}. Resetting.")
    return {"updated_at": None, "collections": {}}


def save_index(chain_key: str, idx: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _index_path(chain_key)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(idx, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"[sweep_api] Failed to save index '{path}': {e}")


# =====================================================================
# COLLECTION REFRESH
# =====================================================================

def refresh_index(
    chain_key: str,
    sweep_haus_chain_id: int,
    max_price_native: Optional[float],
    max_api_pages: int = 3,
    proxy_dict: Optional[dict] = None,
) -> int:
    """
    Fetch active collections from Sweep Haus API and update local index.

    Raises:
        BearerTokenError: on HTTP 401 — propagates up so caller skips chain cleanly.
    """
    lock = _get_index_lock(chain_key)
    with lock:
        idx = load_index(chain_key)
        added = 0
        page = 1
        page_size = 16
        api_contracts: set[str] = set()

        while page <= max_api_pages:
            params = {
                "populate": "cover",
                "filters[isQuest][$eq]": "false",
                "filters[contractDeployer][$notNull]": "true",
                "filters[contractDeployer][$ne]": "",
                "filters[isPublish][$eq]": "true",
                "filters[isActive][$eq]": "true",
                "filters[blockchainMainnet][$in][0]": str(sweep_haus_chain_id),
                "sort": "createdAt:desc",
                "pagination[page]": str(page),
                "pagination[pageSize]": str(page_size),
            }

            try:
                resp = requests.get(
                    SWEEP_API_URL,
                    params=params,
                    headers=_make_headers(),
                    proxies=proxy_dict,
                    timeout=15,
                )

                if resp.status_code == 401:
                    # Hard stop — stale index is worse than no index
                    raise BearerTokenError(
                        f"[sweep_api] Bearer token rejected (401) for chain '{chain_key}'. "
                        "Update BEARER in .env — token may have expired."
                    )

                if resp.status_code != 200:
                    logger.warning(
                        f"[sweep_api] [{chain_key}] API returned {resp.status_code} on page {page}. Stopping."
                    )
                    break

                data = resp.json()
                items = data.get("data", [])
                if not items:
                    break

                for item in items:
                    contract = item.get("NFTCollectionContractAddress")
                    if not contract:
                        continue

                    key = contract.lower()
                    api_contracts.add(key)

                    try:
                        price = float(item.get("price") or 0)
                    except (ValueError, TypeError):
                        price = 0.0

                    if max_price_native is not None and price > max_price_native:
                        continue

                    max_supply = int(item.get("collectionCount") or 0)

                    if key not in idx["collections"]:
                        idx["collections"][key] = {
                            "contract": contract,
                            "name": item.get("name", "Unknown"),
                            "price": price,
                            "max_supply": max_supply,
                            "status": "active",
                            "indexed_at": datetime.now(timezone.utc).isoformat(),
                        }
                        added += 1
                    else:
                        entry = idx["collections"][key]
                        entry["status"] = "active"
                        entry["price"] = price
                        entry["max_supply"] = max_supply

                if len(items) < page_size:
                    break

                page += 1
                time.sleep(0.5)

            except BearerTokenError:
                raise  # propagate — do not swallow
            except requests.RequestException as e:
                logger.warning(f"[sweep_api] [{chain_key}] Request error on page {page}: {e}")
                break

        for key, entry in idx["collections"].items():
            if entry.get("status") == "active" and key not in api_contracts:
                entry["status"] = "removed"

        idx["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_index(chain_key, idx)
        logger.info(
            f"[sweep_api] [{chain_key}] Refreshed: {added} new, "
            f"{len(idx['collections'])} total → sweep_index_{chain_key}.json"
        )
        return added


def get_active_collections(
    chain_key: str,
    sweep_haus_chain_id: int,
    max_price_native: Optional[float],
    index_cache_hours: float,
    max_api_pages: int,
    proxy_dict: Optional[dict] = None,
) -> list[dict]:
    """
    Return active collection list. Refreshes if stale.
    Raises BearerTokenError if token is invalid during refresh.
    """
    idx = load_index(chain_key)
    needs_refresh = True

    updated_at_str = idx.get("updated_at")
    if updated_at_str:
        try:
            updated_at = datetime.fromisoformat(updated_at_str)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - updated_at).total_seconds() / 3600.0
            if hours_since < index_cache_hours:
                needs_refresh = False
        except Exception:
            pass

    if needs_refresh:
        refresh_index(chain_key, sweep_haus_chain_id, max_price_native, max_api_pages, proxy_dict)
        idx = load_index(chain_key)

    return [c for c in idx.get("collections", {}).values() if c.get("status") == "active"]


def mark_collection_status(chain_key: str, contract_address: str, status: str) -> None:
    """Update a collection's status: 'active' | 'sold_out' | 'removed'"""
    lock = _get_index_lock(chain_key)
    with lock:
        idx = load_index(chain_key)
        key = contract_address.lower()
        if key in idx["collections"]:
            idx["collections"][key]["status"] = status
            save_index(chain_key, idx)


# =====================================================================
# MINT STATE TRACKER — one file per wallet
# =====================================================================

def _wallet_minted_path(address: str) -> str:
    """
    Per-wallet mint history file.
    Uses address[:10] (0x + 8 chars) as the filename — unambiguous, filesystem-safe.
    Example: data/minted_0xabcd1234.json
    """
    return os.path.join(DATA_DIR, f"minted_{address.lower()[:10]}.json")


def _get_wallet_lock(address: str) -> threading.Lock:
    key = address.lower()
    with _wallet_locks_meta:
        if key not in _wallet_locks:
            _wallet_locks[key] = threading.Lock()
        return _wallet_locks[key]


def _load_wallet_minted(address: str) -> dict:
    path = _wallet_minted_path(address)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[sweep_api] Corrupt wallet minted file '{path}': {e}. Resetting.")
    return {"address": address.lower(), "minted_contracts": {}, "last_run": None}


def _save_wallet_minted(address: str, data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _wallet_minted_path(address)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.error(f"[sweep_api] Failed to save wallet minted '{path}': {e}")


def get_wallet_record(address: str) -> dict:
    lock = _get_wallet_lock(address)
    with lock:
        return _load_wallet_minted(address)


def record_mint(address: str, chain_key: str, contract_address: str) -> None:
    """Record a successful mint. Keyed by chain_key to allow same contract on different chains."""
    lock = _get_wallet_lock(address)
    with lock:
        data = _load_wallet_minted(address)
        chain_minted = data["minted_contracts"].setdefault(chain_key, [])
        if contract_address.lower() not in chain_minted:
            chain_minted.append(contract_address.lower())
        _save_wallet_minted(address, data)


def set_last_run(address: str) -> None:
    lock = _get_wallet_lock(address)
    with lock:
        data = _load_wallet_minted(address)
        data["last_run"] = datetime.now(timezone.utc).isoformat()
        _save_wallet_minted(address, data)


def is_on_cooldown(address: str, cooldown_hours: float) -> bool:
    record = get_wallet_record(address)
    last_run_str = record.get("last_run")
    if not last_run_str:
        return False
    try:
        last_run = datetime.fromisoformat(last_run_str)
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
        hours_since = (datetime.now(timezone.utc) - last_run).total_seconds() / 3600.0
        if hours_since < cooldown_hours:
            logger.info(
                f"[{address[:8]}] Cooldown: {hours_since:.1f}h elapsed "
                f"(cooldown={cooldown_hours}h). Skipping."
            )
            return True
    except Exception:
        pass
    return False
