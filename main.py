"""
main.py — Entry point for the Sweep Haus NFT auto-minter.

Execution flow:
1. Load .env, validate config files exist
2. Load wallets from pv.txt, proxies from proxy.txt
3. Load bearer tokens into sweep_api
4. Determine active chains via BotConfig.get_active_chains()
5. For each active chain, run all wallets concurrently (up to config.concurrency)
6. Log results summary

[RISK] Concurrent writes to minted state are protected by threading.Lock in sweep_api.
       Index refresh is also locked per chain. These locks are in-process only —
       do not run two instances of this bot against the same data/ directory simultaneously.

[RISK] Private keys are loaded into memory for the session. The process memory is not
       encrypted. Don't run on shared/untrusted hosts.
"""

import logging
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

import sweep_api
from chain_config import BotConfig
from minter import perform_mint
from proxy import get_proxy_for_wallet, load_proxies

# =====================================================================
# LOGGING SETUP
# =====================================================================

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

def setup_logging():
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file (10MB per file, 5 backups)
    fh = RotatingFileHandler(
        LOG_DIR / "sweep_bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


setup_logging()
logger = logging.getLogger("main")

# =====================================================================
# WALLET LOADER
# =====================================================================

PV_FILE = Path(__file__).parent / "pv.txt"

def load_wallets() -> list[tuple[str, str]]:
    """
    Load private keys from pv.txt.
    Returns list of (address, private_key) tuples.
    Lines starting with # are ignored.
    """
    if not PV_FILE.exists():
        logger.error(
            f"pv.txt not found at {PV_FILE}. "
            f"Copy pv.txt.example to pv.txt and add your private keys."
        )
        sys.exit(1)

    from web3 import Web3
    wallets = []
    skipped = 0

    with open(PV_FILE, "r") as f:
        for line_num, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("0x"):
                line = "0x" + line
            try:
                account = Web3().eth.account.from_key(line)
                wallets.append((account.address, line))
            except Exception as e:
                logger.warning(f"[main] pv.txt line {line_num}: invalid key ({e}). Skipping.")
                skipped += 1

    if not wallets:
        logger.error("[main] No valid wallets found in pv.txt. Exiting.")
        sys.exit(1)

    logger.info(f"[main] Loaded {len(wallets)} wallet(s). Skipped {skipped} invalid.")
    return wallets


# =====================================================================
# WORKER (runs in thread per wallet)
# =====================================================================

def wallet_worker(
    wallet_index: int,
    address: str,
    private_key: str,
    chain,
    bot_cfg: BotConfig,
    proxy_dict,
    sweep_fee_wei: int,
) -> dict:
    """Thread worker: mint for one wallet on one chain."""
    result = {
        "address": address,
        "chain": chain.chain_key,
        "success": False,
        "error": None,
    }
    try:
        result["success"] = perform_mint(
            chain=chain,
            address=address,
            private_key=private_key,
            cooldown_hours=bot_cfg.cooldown_hours,
            cooldown_on_fail=bot_cfg.cooldown_on_fail,
            target_mints_range=bot_cfg.target_mints_per_session,
            index_cache_hours=bot_cfg.index_cache_hours,
            max_api_pages=bot_cfg.max_api_pages,
            proxy_dict=proxy_dict,
            sweep_fee_wei=sweep_fee_wei,
        )
    except Exception as e:
        logger.exception(f"[{address[:8]}][{chain.chain_key}] Unhandled error: {e}")
        result["error"] = str(e)
    return result


# =====================================================================
# MAIN
# =====================================================================

SWEEP_FEE_WEI = 202000000000000  # 0.000202 native tokens (Sweep Haus platform fee)


def main():
    # Load .env
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("[main] Loaded .env")
    else:
        logger.warning(
            "[main] .env not found. Bearer tokens must be set in system environment."
        )

    # Load bearer tokens
    try:
        sweep_api.load_bearer_tokens()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    # Load bot config
    try:
        bot_cfg = BotConfig()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    active_chains = bot_cfg.get_active_chains()
    if not active_chains:
        logger.error(
            f"[main] No active chains for mode='{bot_cfg.mode}'. "
            "Check config.json: set 'enabled: true' on chains and verify 'mode' setting."
        )
        sys.exit(1)

    logger.info(
        f"[main] Mode: {bot_cfg.mode} | "
        f"Active chains: {[c.chain_key for c in active_chains]} | "
        f"Concurrency: {bot_cfg.concurrency}"
    )

    # Load wallets and proxies
    wallets = load_wallets()
    proxies = load_proxies()

    # Stats
    total_success = 0
    total_attempted = 0

    # Run each chain sequentially; wallets run concurrently within each chain
    for chain in active_chains:
        logger.info(f"\n{'='*60}")
        logger.info(f"[main] Starting chain: {chain.name} ({chain.chain_key})")
        logger.info(f"{'='*60}")

        # Verify RPC is reachable before spawning threads
        w3 = chain.get_w3()
        if not w3:
            logger.error(f"[main] Skipping {chain.chain_key} — no RPC connection.")
            continue

        tasks = []
        for i, (address, private_key) in enumerate(wallets):
            proxy_dict = get_proxy_for_wallet(proxies, i)
            tasks.append((i, address, private_key, proxy_dict))

        with ThreadPoolExecutor(max_workers=bot_cfg.concurrency) as executor:
            futures = {
                executor.submit(
                    wallet_worker, i, addr, pk, chain, bot_cfg, proxy, SWEEP_FEE_WEI
                ): addr
                for i, addr, pk, proxy in tasks
            }

            for future in as_completed(futures):
                result = future.result()
                total_attempted += 1
                if result["success"]:
                    total_success += 1

                # Jitter between wallet completions
                delay = random.uniform(
                    bot_cfg.delay_between_wallets_sec[0],
                    bot_cfg.delay_between_wallets_sec[1],
                )
                time.sleep(delay)

    logger.info(f"\n{'='*60}")
    logger.info(f"[main] All done. {total_success}/{total_attempted} wallet sessions had successful mints.")
    logger.info(f"{'='*60}\n")


if __name__ == "__main__":
    main()
