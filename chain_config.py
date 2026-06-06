"""
chain_config.py — Loads rpc.json and config.json, merges per-chain settings,
and exposes a clean interface to the rest of the bot.

Design decisions:
- Global config values are defaults; per-chain values override them.
- mode filter ('testnet'/'mainnet'/'all') is applied here, not in minter logic.
- W3 connections are built lazily with RPC fallback.
- [RISK] If rpc.json or config.json are missing, bot exits with a clear message.
"""

import json
import logging
import os
from typing import Optional

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

logger = logging.getLogger("chain_config")

RPC_JSON_PATH = os.path.join(os.path.dirname(__file__), "config", "rpc.json")
CONFIG_JSON_PATH = os.path.join(os.path.dirname(__file__), "config", "config.json")


def _load_json(path: str, label: str) -> dict:
    if not os.path.exists(path):
        example = path.replace(".json", ".json.example")
        raise FileNotFoundError(
            f"[chain_config] Missing {label}: {path}\n"
            f"Copy the example: cp {example} {path}"
        )
    with open(path, "r") as f:
        raw = f.read()
    # Strip comment keys before parsing (keys starting with _comment)
    import re
    raw = re.sub(r'"_comment[^"]*"\s*:\s*"[^"]*",?\n?', '', raw)
    return json.loads(raw)


def _get(chain_cfg: dict, global_cfg: dict, key: str, default=None):
    """Per-chain value overrides global. Falls back to default."""
    return chain_cfg.get(key, global_cfg.get(key, default))


class ChainConfig:
    """Parsed, merged config for a single chain."""

    def __init__(self, chain_key: str, rpc_entry: dict, chain_mint_cfg: dict, global_cfg: dict):
        self.chain_key = chain_key
        self.chain_id: int = rpc_entry["chain_id"]
        self.name: str = rpc_entry["name"]
        self.type: str = rpc_entry.get("type", "mainnet")  # 'mainnet' | 'testnet'
        self.rpc_urls: list[str] = rpc_entry["rpc"]
        self.explorer: str = rpc_entry.get("explorer", "")
        self.sweep_haus_chain_id: int = rpc_entry.get("sweep_haus_chain_id", self.chain_id)
        self.native_symbol: str = rpc_entry.get("native_symbol", "ETH")
        self.native_currency_address: str = rpc_entry.get(
            "native_currency_address", "0xEeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
        )
        # Native currency as int for calldata
        self.native_currency_int: int = int(
            self.native_currency_address.lower().replace("0x", ""), 16
        )

        # Mint settings (per-chain overrides global)
        g = global_cfg
        c = chain_mint_cfg
        self.enabled: bool = c.get("enabled", False)
        self.max_price_native: Optional[float] = _get(c, g, "max_price_native", None)
        self.max_price_usd_equiv: Optional[float] = _get(c, g, "max_price_usd_equiv", None)
        # gas_price_gwei: None or 0 both mean "use on-chain eth_gasPrice dynamically"
        # Beginner-friendly: setting 0 = auto, same as null
        _gwei = _get(c, g, "gas_price_gwei", None)
        self.gas_price_gwei: Optional[float] = None if (_gwei is None or _gwei == 0) else float(_gwei)
        self.gas_limit: int = _get(c, g, "gas_limit", 280000)
        self.gas_multiplier: float = _get(c, g, "gas_multiplier", 1.2)
        self.priority_multiplier: float = _get(c, g, "priority_multiplier", 1.1)
        # gas_buffer_units: gas unit count reserved for fee estimation in balance check.
        # Buffer in wei = gas_buffer_units * base_gas_price (computed in minter.py)
        # Named "units" not "gwei" — it is multiplied BY gas price, it is not itself a price.
        self.gas_buffer_units: int = _get(c, g, "gas_buffer_units", 350000)

        self._w3: Optional[Web3] = None

    @property
    def native_currency_wei(self) -> int:
        return self.native_currency_int

    def get_w3(self) -> Optional[Web3]:
        """Return a connected Web3 instance, trying RPCs in order."""
        if self._w3 and self._w3.is_connected():
            return self._w3

        for rpc_url in self.rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
                w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
                if w3.is_connected():
                    detected = w3.eth.chain_id
                    if detected != self.chain_id:
                        logger.warning(
                            f"[{self.chain_key}] RPC {rpc_url} returned chain_id {detected}, "
                            f"expected {self.chain_id}. Skipping."
                        )
                        continue
                    self._w3 = w3
                    logger.info(f"[{self.chain_key}] Connected via {rpc_url}")
                    return w3
            except Exception as e:
                logger.warning(f"[{self.chain_key}] RPC {rpc_url} failed: {e}")

        logger.error(f"[{self.chain_key}] All RPCs failed. Chain skipped.")
        return None

    def __repr__(self):
        return f"<ChainConfig {self.chain_key} chain_id={self.chain_id} type={self.type} enabled={self.enabled}>"


class BotConfig:
    """Top-level config object loaded at startup."""

    def __init__(self):
        rpc_data = _load_json(RPC_JSON_PATH, "rpc.json")
        cfg_data = _load_json(CONFIG_JSON_PATH, "config.json")

        global_cfg = cfg_data.get("global", {})
        chains_mint_cfg = cfg_data.get("chains", {})
        chains_rpc = rpc_data.get("chains", {})

        self.mode: str = global_cfg.get("mode", "testnet")
        self.concurrency: int = global_cfg.get("concurrency", 3)
        self.delay_between_wallets_sec: list = global_cfg.get("delay_between_wallets_sec", [5, 15])
        self.cooldown_hours: float = global_cfg.get("cooldown_hours", 24.0)
        self.cooldown_on_fail: bool = global_cfg.get("cooldown_on_fail", False)
        self.target_mints_per_session: list = global_cfg.get("target_mints_per_session", [1, 5])
        self.index_cache_hours: float = global_cfg.get("index_cache_hours", 6.0)
        self.max_api_pages: int = global_cfg.get("max_api_pages", 3)

        # Build ChainConfig objects for each chain in rpc.json
        self._chains: dict[str, ChainConfig] = {}
        for chain_key, rpc_entry in chains_rpc.items():
            chain_mint_cfg = chains_mint_cfg.get(chain_key, {})
            cc = ChainConfig(chain_key, rpc_entry, chain_mint_cfg, global_cfg)
            self._chains[chain_key] = cc

    def get_active_chains(self) -> list[ChainConfig]:
        """
        Returns chains that are:
        - enabled: true in config.json
        - match the mode filter (testnet/mainnet/all)
        """
        result = []
        for cc in self._chains.values():
            if not cc.enabled:
                continue
            if self.mode == "testnet" and cc.type != "testnet":
                continue
            if self.mode == "mainnet" and cc.type != "mainnet":
                continue
            result.append(cc)

        if not result:
            logger.warning(
                f"[BotConfig] No active chains found for mode='{self.mode}'. "
                f"Check config.json: set 'enabled: true' for chains matching this mode."
            )
        return result

    def __repr__(self):
        chains = list(self._chains.keys())
        return f"<BotConfig mode={self.mode} chains={chains}>"
