"""
proxy.py — Loads proxy list and assigns one proxy per wallet (index-based rotation).

Rotation strategy: wallet index % len(proxies)
- This means wallet 0 always uses proxy 0, wallet 1 uses proxy 1, etc.
- Keeps assignments stable across runs so the same proxy/wallet pair isn't flagged.
- If you have fewer proxies than wallets, proxies wrap around.

[RISK] Proxies are not validated on load — a dead proxy will surface as a request error
at mint time, not at startup. Add a health-check loop if you need pre-validation.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger("proxy")

PROXY_FILE = os.path.join(os.path.dirname(__file__), "proxy.txt")


def load_proxies() -> list[str]:
    """Load proxies from proxy.txt. Returns empty list if file missing."""
    if not os.path.exists(PROXY_FILE):
        logger.info("[proxy] proxy.txt not found — running without proxies.")
        return []

    proxies = []
    with open(PROXY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            proxies.append(line)

    if proxies:
        logger.info(f"[proxy] Loaded {len(proxies)} proxies.")
    else:
        logger.info("[proxy] proxy.txt is empty — running without proxies.")
    return proxies


def get_proxy_for_wallet(proxies: list[str], wallet_index: int) -> Optional[dict]:
    """
    Returns a requests-compatible proxy dict for the given wallet index.
    Returns None if no proxies are configured.
    """
    if not proxies:
        return None
    proxy_url = proxies[wallet_index % len(proxies)]
    return {"http": proxy_url, "https": proxy_url}
