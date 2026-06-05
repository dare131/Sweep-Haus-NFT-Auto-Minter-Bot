"""
sweep_api.py — Sweep Haus API client, per-chain index caching, and collection refresh.

Index files are keyed by chain_key (human-readable string from rpc.json), not by
numeric chain ID. This means:
  data/sweep_index_x1_testnet.json
  data/sweep_index_base_mainnet.json
  ...instead of sweep_index_10778.json

This makes manual inspection and debugging straightforward — you can open the file
for a specific chain without cross-referencing chain IDs.

The sweep_haus_chain_id (int) is still passed to the API query params — it's the
filter value Sweep Haus uses internally. It is NOT used as a file/lock key.

Bearer token rotation: tokens are loaded from env and cycled round-robin per API call.
Index cache: per-chain JSON files in data/ directory, refreshed every N hours (configurable).

[RISK] The Sweep Haus API is undocumented/private. The Bearer token is required for
authenticated requests. If the auth scheme changes, this module breaks silently unless
status codes are checked — which they are here.

[RISK] Concurrent refreshes across chains are independent (one lock per chain_key).
Multiple wallets on the same chain share one refresh cycle via the cache TTL check.
"""

import json
import logging
import os
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

# Per-chain index locks (created on demand)
_index_locks: dict[str, threading.Lock] = {}
_index_locks_meta = threading.Lock()


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

    # Check BEARER_1, BEARER_2, ... first
    i = 1
    while True:
        val = os.environ.get(f"BEARER_{i}", "").strip()
        if not val:
            break
        tokens.append(val)
        i += 1

    # Fall back to single BEARER
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
    """Return the next bearer token in round-robin rotation."""
    global _bearer_index
    with _bearer_lock:
        token = _BEARER_TOKENS[_bearer_index % len(_BEARER_TOKENS)]
        _bearer_index += 1
    return token


def _make_headers() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://sweep.haus/",
        "Origin": "https://sweep.haus",
        "Authorization": f"Bearer {_next_bearer()}",
    }


# =====================================================================
# INDEX FILE HELPERS
# =====================================================================

def _index_path(chain_key: str) -> str:
    """
    Returns path like: data/sweep_index_x1_testnet.json
    chain_key comes from rpc.json top-level key (e.g. 'x1_testnet', 'base_mainnet').
    """
    return os.path.join(DATA_DIR, f"sweep_index_{chain_key}.json")


def _minted_path() -> str:
    return os.path.join(DATA_DIR, "sweep_minted.json")


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
            logger.warning(f"[sweep_api] Corrupt index file '{path}': {e}. Resetting.")
    return {"updated_at": None, "collections": {}}


def save_index(chain_key: str, idx: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _index_path(chain_key)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(idx, f, indent=2)
        os.replace(tmp_path, path)  # Atomic write
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

    Args:
        chain_key:           Human-readable key from rpc.json (e.g. 'x1_testnet').
                             Used as the index file name and lock key.
        sweep_haus_chain_id: Numeric chain ID sent to the Sweep Haus API as a filter.
        max_price_native:    Max price filter (native token). None = include all.
        max_api_pages:       Max pages to fetch (16 items/page).
        proxy_dict:          Optional proxy dict for requests.

    Returns:
        Number of newly added collections.
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
                    logger.error("[sweep_api] Bearer token rejected (401). Check your .env tokens.")
                    break
                if resp.status_code != 200:
                    logger.warning(f"[sweep_api] API returned {resp.status_code} on page {page}.")
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

                    # Price filter
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

            except requests.RequestException as e:
                logger.warning(f"[sweep_api] Request error on page {page}: {e}")
                break

        # Mark collections that disappeared from API as removed
        for key, entry in idx["collections"].items():
            if entry.get("status") == "active" and key not in api_contracts:
                entry["status"] = "removed"

        idx["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_index(chain_key, idx)
        logger.info(
            f"[sweep_api] [{chain_key}] Index refreshed: "
            f"{added} new, {len(idx['collections'])} total. "
            f"File: sweep_index_{chain_key}.json"
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
    Return active collection list for a chain.
    Refreshes index file (sweep_index_{chain_key}.json) if cache is stale or missing.
    """
    idx = load_index(chain_key)
    updated_at_str = idx.get("updated_at")
    needs_refresh = True

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
    """
    Update a single collection's status in its chain index file.
    Status options: 'active', 'sold_out', 'removed'
    """
    lock = _get_index_lock(chain_key)
    with lock:
        idx = load_index(chain_key)
        key = contract_address.lower()
        if key in idx["collections"]:
            idx["collections"][key]["status"] = status
            save_index(chain_key, idx)


# =====================================================================
# MINT STATE TRACKER
# =====================================================================

_minted_lock = threading.Lock()


def load_minted() -> dict:
    path = _minted_path()
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[sweep_api] Corrupt minted file: {e}. Resetting.")
    return {}


def save_minted(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    path = _minted_path()
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.error(f"[sweep_api] Failed to save minted state: {e}")


def get_wallet_record(address: str) -> dict:
    with _minted_lock:
        data = load_minted()
        return data.get(address.lower(), {"minted_contracts": {}, "last_run": None})


def record_mint(address: str, chain_key: str, contract_address: str) -> None:
    """
    Record a successful mint for address + chain + contract.
    Keyed by chain_key to allow same contract on different chains.
    """
    with _minted_lock:
        data = load_minted()
        wallet = data.setdefault(address.lower(), {"minted_contracts": {}, "last_run": None})
        chain_minted = wallet["minted_contracts"].setdefault(chain_key, [])
        if contract_address.lower() not in chain_minted:
            chain_minted.append(contract_address.lower())
        save_minted(data)


def set_last_run(address: str) -> None:
    """Set the last run timestamp for a wallet."""
    with _minted_lock:
        data = load_minted()
        wallet = data.setdefault(address.lower(), {"minted_contracts": {}, "last_run": None})
        wallet["last_run"] = datetime.now(timezone.utc).isoformat()
        save_minted(data)


def is_on_cooldown(address: str, cooldown_hours: float) -> bool:
    """Check if wallet is within its cooldown window."""
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
                f"[{address[:8]}] Cooldown: {hours_since:.1f}h since last run "
                f"(cooldown={cooldown_hours}h). Skipping."
            )
            return True
    except Exception:
        pass
    return False
