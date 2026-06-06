"""
main.py — Entry point for the Sweep Haus NFT auto-minter.

Execution flow:
1. Parse CLI args (--dry-run, --loop, --loop-interval)
2. Load .env, validate bearer tokens and config files
3. Load wallets from pv.txt, proxies from proxy.txt
4. Determine active chains via mode filter
5. For each active chain, run wallets concurrently (up to config.concurrency)
6. If --loop is set, sleep and repeat every N hours

Usage:
  python main.py                          # single run, live txs
  python main.py --dry-run               # single run, no txs broadcast
  python main.py --loop                  # loop every 24h (from config)
  python main.py --loop --loop-interval 12   # loop every 12h
  python main.py --dry-run --loop        # dry-run loop (safe for testing)

Env vars (alternative to CLI for loop mode):
  LOOP=true
  LOOP_INTERVAL_HOURS=24

[RISK] Locks are in-process only. Do not run two bot instances against the same data/.
[RISK] Private keys are in process memory for the session duration.
"""

import argparse
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
from sweep_api import BearerTokenError
from chain_config import BotConfig
from minter import perform_mint
from proxy import get_proxy_for_wallet, load_proxies

# =====================================================================
# LOGGING SETUP
# =====================================================================

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


def setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    root.addHandler(ch)

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
    Keys are used inside worker threads and then go out of scope.
    """
    if not PV_FILE.exists():
        logger.error(
            f"pv.txt not found at {PV_FILE}. "
            "Copy pv.txt.example to pv.txt and add your private keys."
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
# WORKER (runs in thread pool per chain)
# =====================================================================

SWEEP_FEE_WEI = 202000000000000  # 0.000202 native tokens (Sweep Haus platform fee)


def wallet_worker(
    wallet_index: int,
    address: str,
    private_key: str,
    chain,
    bot_cfg: BotConfig,
    proxy_dict,
    dry_run: bool,
) -> dict:
    """Thread worker: run mint session for one wallet on one chain."""
    result = {
        "address": address,
        "chain": chain.chain_key,
        "success": False,
        "bearer_error": False,
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
            dry_run=dry_run,
            proxy_dict=proxy_dict,
            sweep_fee_wei=SWEEP_FEE_WEI,
        )
    except BearerTokenError as e:
        logger.error(str(e))
        result["bearer_error"] = True
    except Exception as e:
        logger.exception(f"[{address[:8]}][{chain.chain_key}] Unhandled error: {e}")
        result["error"] = str(e)
    return result


# =====================================================================
# SINGLE RUN
# =====================================================================

def run_once(bot_cfg: BotConfig, wallets: list, proxies: list, dry_run: bool) -> dict:
    """
    Execute one full minting pass across all active chains and wallets.
    Returns summary stats dict.
    """
    active_chains = bot_cfg.get_active_chains()
    if not active_chains:
        logger.error(
            f"[main] No active chains for mode='{bot_cfg.mode}'. "
            "Check config.json: set 'enabled: true' and verify 'mode'."
        )
        return {"total_success": 0, "total_attempted": 0}

    total_success = 0
    total_attempted = 0

    for chain in active_chains:
        logger.info(f"\n{'='*60}")
        logger.info(f"[main] Chain: {chain.name} ({chain.chain_key})" + (" [DRY-RUN]" if dry_run else ""))
        logger.info(f"{'='*60}")

        w3 = chain.get_w3()
        if not w3:
            logger.error(f"[main] Skipping {chain.chain_key} — no RPC connection.")
            continue

        tasks = [
            (i, addr, pk, get_proxy_for_wallet(proxies, i))
            for i, (addr, pk) in enumerate(wallets)
        ]

        bearer_failed = False

        with ThreadPoolExecutor(max_workers=bot_cfg.concurrency) as executor:
            futures = {
                executor.submit(
                    wallet_worker, i, addr, pk, chain, bot_cfg, proxy, dry_run
                ): addr
                for i, addr, pk, proxy in tasks
            }

            for future in as_completed(futures):
                result = future.result()
                total_attempted += 1

                if result["bearer_error"]:
                    # One wallet hit 401 — all others on this chain will too.
                    # Cancel remaining work for this chain.
                    bearer_failed = True
                    for f in futures:
                        f.cancel()
                    break

                if result["success"]:
                    total_success += 1

                delay = random.uniform(
                    bot_cfg.delay_between_wallets_sec[0],
                    bot_cfg.delay_between_wallets_sec[1],
                )
                time.sleep(delay)

        if bearer_failed:
            logger.error(
                f"[main] Bearer token rejected for chain '{chain.chain_key}'. "
                "Skipping remaining wallets on this chain. Update BEARER in .env."
            )

    return {"total_success": total_success, "total_attempted": total_attempted}


# =====================================================================
# ENTRY POINT
# =====================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sweep Haus NFT Auto-Minter Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                        Single run, live transactions
  python main.py --dry-run              Single run, no transactions sent
  python main.py --loop                 Repeat every 24h (from config)
  python main.py --loop --interval 12   Repeat every 12h
  python main.py --dry-run --loop       Safe loop for testing config
        """,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline but skip actual transaction broadcast. Safe for testing.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=os.environ.get("LOOP", "false").lower() == "true",
        help="Loop indefinitely. Sleeps between runs. (env: LOOP=true)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("LOOP_INTERVAL_HOURS", "24")),
        metavar="HOURS",
        help="Hours between loop iterations. Default: 24. (env: LOOP_INTERVAL_HOURS)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load .env
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("[main] Loaded .env")
    else:
        logger.warning("[main] .env not found — bearer tokens must be in system environment.")

    # Bearer tokens
    try:
        sweep_api.load_bearer_tokens()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    # Bot config
    try:
        bot_cfg = BotConfig()
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info(
        f"[main] Mode: {bot_cfg.mode} | "
        f"Concurrency: {bot_cfg.concurrency} | "
        f"Dry-run: {args.dry_run} | "
        f"Loop: {args.loop} | "
        f"Interval: {args.interval}h"
    )

    if args.dry_run:
        logger.info("[main] *** DRY-RUN MODE — no transactions will be broadcast ***")

    wallets = load_wallets()
    proxies = load_proxies()

    run_number = 0

    while True:
        run_number += 1
        logger.info(f"\n{'#'*60}")
        logger.info(f"[main] === RUN #{run_number} ===")
        logger.info(f"{'#'*60}")

        stats = run_once(bot_cfg, wallets, proxies, args.dry_run)

        logger.info(
            f"[main] Run #{run_number} complete. "
            f"{stats['total_success']}/{stats['total_attempted']} sessions had successful mints."
        )

        if not args.loop:
            break

        next_run_secs = args.interval * 3600
        logger.info(
            f"[main] Sleeping {args.interval}h until next run. "
            f"Press Ctrl+C to stop."
        )
        try:
            time.sleep(next_run_secs)
        except KeyboardInterrupt:
            logger.info("[main] Interrupted by user. Exiting.")
            break


if __name__ == "__main__":
    main()
