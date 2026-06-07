"""
main.py — Entry point for the Sweep Haus NFT auto-minter. Final version.

Anti-fingerprint:
  - Staggered wallet launch (delay_between_wallets_sec between each submit)
  - Loop jitter ±10% on sleep interval

Reliability for unattended loop mode:
  - Telegram alerts on bearer expiry and RPC failure (optional, see notify.py)
  - Run summary alert after each loop iteration
  - Startup alert confirms bot is live
  - Config reloaded each run — change config.json between runs, no restart needed
  - RPC health checked per-chain before spawning threads
  - SWEEP_FEE_WEI overridable via SWEEP_FEE env var
  - bearer_failed propagated to stats so loop can report it

Usage:
  python main.py                        single run, live txs
  python main.py --dry-run              single run, no txs broadcast
  python main.py --loop                 loop every 24h (from config)
  python main.py --loop --interval 12   loop every 12h
  python main.py --dry-run --loop       safe testing loop

Env vars:
  LOOP=true
  LOOP_INTERVAL_HOURS=24
  SWEEP_FEE=202000000000000     override platform fee in wei (optional)
  TELEGRAM_BOT_TOKEN=...        enable Telegram alerts (optional)
  TELEGRAM_CHAT_ID=...          enable Telegram alerts (optional)

[RISK] Locks are in-process only. Do not run two instances against the same data/.
[RISK] Private keys are in process memory for the session duration.
"""

import argparse
import logging
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

import sweep_api
from sweep_api import BearerTokenError
from chain_config import BotConfig
from minter import perform_mint
from notify import alert_bearer_expired, alert_rpc_down, alert_run_summary, alert_startup, init_notifications, alert_heartbeat
from proxy import get_proxy_for_wallet, load_proxies

# =====================================================================
# PERIODIC HEARTBEAT TRACKING
# =====================================================================
_START_TIME = time.time()
_LAST_RUN_STATS = None
_NEXT_RUN_TIME = None
_CURRENT_STATE = "Initializing"


def heartbeat_loop(wallet_count: int) -> None:
    """Sends a Telegram heartbeat update every 5 minutes."""
    # Give the startup alert a moment to send first
    time.sleep(10)
    
    while True:
        # Calculate uptime
        uptime_secs = int(time.time() - _START_TIME)
        days, remainder = divmod(uptime_secs, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if days > 0:
            uptime_str = f"{days}d {hours}h {minutes}m"
        else:
            uptime_str = f"{hours}h {minutes}m"
        
        # Build last run summary
        if _LAST_RUN_STATS:
            success = _LAST_RUN_STATS.get("success", 0)
            attempted = _LAST_RUN_STATS.get("attempted", 0)
            run_num = _LAST_RUN_STATS.get("run_number", 0)
            ts_str = _LAST_RUN_STATS.get("timestamp", "")
            last_run_summary = f"Run #{run_num} complete ({success}/{attempted} sessions) at {ts_str}"
        else:
            last_run_summary = "None yet"
            
        # Build next run summary
        if _NEXT_RUN_TIME:
            secs_left = int(_NEXT_RUN_TIME - time.time())
            if secs_left <= 0:
                next_run_summary = "Starting now..."
            else:
                left_hours, left_rem = divmod(secs_left, 3600)
                left_mins, _ = divmod(left_rem, 60)
                next_run_summary = f"Scheduled in {left_hours}h {left_mins}m"
        else:
            next_run_summary = "Not scheduled"
            
        try:
            alert_heartbeat(
                status=_CURRENT_STATE,
                uptime_str=uptime_str,
                wallet_count=wallet_count,
                last_run_summary=last_run_summary,
                next_run_summary=next_run_summary
            )
        except Exception as e:
            logger.debug(f"[main] Heartbeat send failed: {e}")
            
        # Sleep for 5 minutes (300 seconds)
        time.sleep(300)

# =====================================================================
# LOGGING SETUP
# =====================================================================

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)


class ColorFormatter(logging.Formatter):
    """Custom color formatter using ANSI escape sequences for terminal output."""
    GREY = "\033[90m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    LEVEL_COLORS = {
        logging.DEBUG: GREY,
        logging.INFO: BLUE,
        logging.WARNING: YELLOW,
        logging.ERROR: RED,
        logging.CRITICAL: RED + BOLD,
    }

    def format(self, record: logging.LogRecord) -> str:
        # Format the time
        asctime = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        asctime_colored = f"{self.GREY}{asctime}{self.RESET}"

        # Color the level name
        color = self.LEVEL_COLORS.get(record.levelno, self.RESET)
        levelname_colored = f"{color}{record.levelname:<8}{self.RESET}"

        # Color the logger name
        name_colored = f"{self.CYAN}{record.name}{self.RESET}"

        # Format the message body
        msg_str = record.getMessage()

        # Color checkmarks green
        if "✓" in msg_str:
            msg_str = msg_str.replace("✓", f"{self.GREEN}✓{self.RESET}")

        # Color addresses (0x...) in magenta
        import re
        msg_str = re.sub(
            r'(0x[a-fA-F0-9]{4})[a-fA-F0-9]+([a-fA-F0-9]{4})',
            f'{self.MAGENTA}\\1...\\2{self.RESET}',
            msg_str
        )
        # Handle transaction hashes and contract addresses
        msg_str = re.sub(
            r'(0x[a-fA-F0-9]{6})[a-fA-F0-9]{30,}([a-fA-F0-9]{4})?',
            f'{self.MAGENTA}\\1...{self.RESET}',
            msg_str
        )

        # Highlight "dry-run" in bold yellow
        if "DRY-RUN" in msg_str or "dry-run" in msg_str.lower():
            msg_str = re.sub(
                r'(?i)(dry-run)',
                f'{self.YELLOW}{self.BOLD}\\1{self.RESET}',
                msg_str
            )

        # Highlight successful mints / runs in green
        if "Minted" in msg_str or "Simulated" in msg_str:
            msg_str = msg_str.replace("Minted", f"{self.GREEN}{self.BOLD}Minted{self.RESET}")
            msg_str = msg_str.replace("Simulated", f"{self.GREEN}{self.BOLD}Simulated{self.RESET}")

        # Highlight errors / fails in red
        if "Failed" in msg_str or "failed" in msg_str:
            msg_str = msg_str.replace("Failed", f"{self.RED}Failed{self.RESET}")
            msg_str = msg_str.replace("failed", f"{self.RED}failed{self.RESET}")
        if "revert" in msg_str.lower():
            msg_str = re.sub(
                r'(?i)(revert(ed)?)',
                f'{self.RED}{self.BOLD}\\1{self.RESET}',
                msg_str
            )

        # Highlight chain key in brackets (e.g. [arc_testnet]) in bold cyan.
        # Uses negative lookbehind to avoid matching bracket character inside ANSI escape sequences.
        msg_str = re.sub(
            r'(?<!\033)\[([a-zA-Z0-9_#-]+)\]',
            f'[{self.CYAN}\\1{self.RESET}]',
            msg_str
        )

        formatted = f"{asctime_colored} | {levelname_colored} | {name_colored} | {msg_str}"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            formatted += f"\n{self.RED}{record.exc_text}{self.RESET}"

        return formatted


def setup_logging() -> None:
    # Enable virtual terminal support on Windows if possible
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(ColorFormatter())
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
    """Load private keys from pv.txt. Returns (address, private_key) tuples."""
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
        logger.error("[main] No valid wallets in pv.txt. Exiting.")
        sys.exit(1)

    logger.info(f"[main] Loaded {len(wallets)} wallet(s). Skipped {skipped} invalid.")
    return wallets





# =====================================================================
# WORKER
# =====================================================================

def wallet_worker(
    wallet_index: int,
    address: str,
    private_key: str,
    chain,
    bot_cfg: BotConfig,
    proxy_dict,
    dry_run: bool,
    collections: list[dict],
    force_run: bool,
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
            collections=collections,
            dry_run=dry_run,
            proxy_dict=proxy_dict,
            sweep_fee_wei=chain.sweep_fee_wei,  # per-chain from rpc.json
            force_run=force_run,
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

def run_once(
    bot_cfg: BotConfig,
    wallets: list,
    proxies: list,
    dry_run: bool,
    force_run: bool = False,
    interval_hours: float = 24.0,
    is_loop: bool = False,
) -> dict:
    """
    One full minting pass across all active chains and wallets.
    Config is read from bot_cfg which is reloaded per run by the caller.

    Returns stats dict including bearer_failed flag so the loop can alert properly.
    """
    active_chains = bot_cfg.get_active_chains()
    if not active_chains:
        logger.error(
            f"[main] No active chains for mode='{bot_cfg.mode}'. "
            "Check config.json: 'enabled: true' + correct 'mode'."
        )
        return {"total_success": 0, "total_attempted": 0, "bearer_failed": False}

    # 1. Verify uniqueness of chain_id to prevent nonce conflicts in global interleaving
    unique_chains = []
    seen_chain_ids = set()
    for chain in active_chains:
        if chain.chain_id in seen_chain_ids:
            logger.error(f"[main] Skipping {chain.chain_key} due to duplicate chain ID {chain.chain_id} (nonce conflict risk).")
            continue
        seen_chain_ids.add(chain.chain_id)
        unique_chains.append(chain)

    healthy_chains = []
    chain_collections = {}
    any_bearer_failed = False

    # 2. Pre-validate RPC status & Fetch collection lists in the main thread
    for chain in unique_chains:
        logger.info(f"\n{'='*60}")
        logger.info(
            f"[main] Initializing Chain: {chain.name} ({chain.chain_key})"
            + (" [DRY-RUN]" if dry_run else "")
        )
        logger.info(f"{'='*60}")

        w3 = chain.get_w3()
        if not w3:
            logger.error(f"[main] Skipping {chain.chain_key} — no RPC connection.")
            alert_rpc_down(chain.chain_key, chain.rpc_urls)
            continue

        # Print estimated costs at startup
        try:
            live_gas_price = w3.eth.gas_price
            fee_native  = float(w3.from_wei(chain.sweep_fee_wei, "ether"))
            gas_native  = float(w3.from_wei(live_gas_price * chain.gas_limit, "ether"))
            total_native = fee_native + gas_native
            sep = "-" * 56
            logger.info(f"[main] {sep}")
            logger.info(f"[main] Cost per FREE mint on {chain.name}")
            logger.info(f"[main] {sep}")
            logger.info(f"[main]   Platform fee (Sweep Haus) : {fee_native:.8f} {chain.native_symbol}")
            logger.info(f"[main]   Est. gas (~{chain.gas_limit:,} units) : {gas_native:.8f} {chain.native_symbol}")
            logger.info(f"[main]   Total per free NFT        : {total_native:.8f} {chain.native_symbol}")
            logger.info(f"[main] {sep}")
            logger.info("[main]   NOTE: Paid NFTs cost extra on top of the above.")
            logger.info("[main]   Gas varies with network — this is an estimate.")
            logger.info(f"[main] {sep}")
        except Exception as e:
            logger.debug(f"[main] Could not compute fee summary: {e}")

        # Resolve collections (handle manual list vs API)
        if chain.manual_collections:
            logger.info(f"[main] [{chain.chain_key}] Using {len(chain.manual_collections)} manual collections from config.")
            cols = []
            for item in chain.manual_collections:
                contract_addr = item.get("contract")
                if contract_addr:
                    cols.append({
                        "contract": contract_addr,
                        "name": item.get("name", "Unknown"),
                        "price": float(item.get("price", 0.0)),
                        "max_supply": int(item.get("max_supply", 0)),
                        "status": "active"
                    })
            chain_collections[chain.chain_key] = cols
            healthy_chains.append(chain)
        else:
            try:
                # API load (cache lookup or refresh) in the main thread.
                # If BEARER expired, this will raise BearerTokenError.
                cols = sweep_api.get_active_collections(
                    chain_key=chain.chain_key,
                    sweep_haus_chain_id=chain.sweep_haus_chain_id,
                    max_price_native=chain.max_price_native,
                    index_cache_hours=bot_cfg.index_cache_hours,
                    max_api_pages=bot_cfg.max_api_pages,
                    proxy_dict=None,  # Main thread refresh doesn't require proxy
                )
                chain_collections[chain.chain_key] = cols
                healthy_chains.append(chain)
            except BearerTokenError as e:
                logger.error(
                    f"[main] Bearer token rejected for '{chain.chain_key}'. "
                    f"Skipping this chain. Error: {e}"
                )
                alert_bearer_expired(chain.chain_key)
                any_bearer_failed = True

    if not healthy_chains:
        logger.error("[main] No healthy chains remaining to process.")
        return {
            "total_success": 0,
            "total_attempted": 0,
            "bearer_failed": any_bearer_failed,
        }

    # 3. Build a single flat list of tasks across all healthy chains and wallets
    tasks = []
    for chain in healthy_chains:
        cols = chain_collections[chain.chain_key]
        for i, (addr, pk) in enumerate(wallets):
            tasks.append((i, addr, pk, chain, get_proxy_for_wallet(proxies, i), cols))

    # Shuffle all tasks to interleave wallets and chains globally
    random.shuffle(tasks)

    # 4. Calculate dynamic adaptive delay
    total_tasks = len(tasks)
    if dry_run:
        delay_min = 0.01
        delay_max = 0.05
    else:
        # Submission launch window target: 10 minutes (600s) for single-run, 50% of loop interval for loop
        if is_loop:
            total_window = interval_hours * 3600.0 * 0.5
        else:
            total_window = 600.0  # 10 minutes (submission window, not completion window)

        target_avg = total_window / max(1, total_tasks)
        adaptive_min = target_avg * 0.5
        adaptive_max = target_avg * 1.5

        cfg_min, cfg_max = bot_cfg.delay_between_wallets_sec[0], bot_cfg.delay_between_wallets_sec[1]
        delay_min = max(0.5, min(adaptive_min, cfg_max))
        delay_max = max(0.5, min(adaptive_max, cfg_max))

        if delay_min > delay_max:
            delay_min = delay_max

    logger.info(
        f"[main] Launching {total_tasks} tasks globally across {len(healthy_chains)} chain(s) "
        f"using adaptive stagger delay range: [{delay_min:.2f}s, {delay_max:.2f}s]."
    )

    total_success = 0
    total_attempted = 0
    futures: dict = {}

    # Pool size: concurrency per active chain * count of healthy chains
    max_workers = bot_cfg.concurrency * len(healthy_chains)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, (i, addr, pk, chain, proxy, cols) in enumerate(tasks):
            if idx > 0:
                stagger = random.uniform(delay_min, delay_max)
                logger.debug(f"[main] Stagger: next task launch in {stagger:.2f}s")
                time.sleep(stagger)

            future = executor.submit(
                wallet_worker, i, addr, pk, chain, bot_cfg, proxy, dry_run, cols, force_run
            )
            futures[future] = addr

        for future in as_completed(futures):
            try:
                result = future.result()
                total_attempted += 1
                if result["success"]:
                    total_success += 1
            except Exception as e:
                logger.exception(f"[main] Task execution exception: {e}")

    return {
        "total_success": total_success,
        "total_attempted": total_attempted,
        "bearer_failed": any_bearer_failed,
    }


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
        help="Full pipeline but no transactions broadcast.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="WARNING: bypasses anti-fingerprint session skip and daily cooldown. Do not use in unattended --loop mode.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        default=os.environ.get("LOOP", "false").lower() == "true",
        help="Loop indefinitely with jittered sleep. (env: LOOP=true)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("LOOP_INTERVAL_HOURS", "24")),
        metavar="HOURS",
        help="Base hours between loop iterations. Default: 24. (env: LOOP_INTERVAL_HOURS)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # Load .env first — everything else depends on it
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logger.info("[main] Loaded .env")
    else:
        logger.warning("[main] .env not found — env vars must be set in system environment.")

    # Init alerting (silent no-op if TELEGRAM_* not set)
    init_notifications()

    # Bearer tokens
    try:
        sweep_api.load_bearer_tokens()
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)

    # sweep_fee_wei is now per-chain — set in rpc.json as "sweep_fee_wei" per chain.

    # Initial config load
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
        f"Interval: {args.interval}h ±10% | "
        f"Sweep fee: per-chain (see sweep_fee_wei in rpc.json)"
    )

    if args.dry_run:
        logger.info("[main] *** DRY-RUN — no transactions will be broadcast ***")

    if args.force and args.loop:
        warn_msg = "WARNING: Both --force and --loop are active. Bypassing anti-fingerprint session skips and daily cooldowns in unattended loop mode is HIGH RISK for Sybil detection!"
        print(f"\n{warn_msg}\n", file=sys.stderr)
        logger.warning(warn_msg)

    wallets = load_wallets()
    proxies = load_proxies()

    # Send startup alert when running unattended in loop mode
    if args.loop:
        active_chains = bot_cfg.get_active_chains()
        alert_startup(
            mode=bot_cfg.mode,
            chains=[c.chain_key for c in active_chains],
            wallet_count=len(wallets),
            dry_run=args.dry_run,
        )
        hb_thread = threading.Thread(target=heartbeat_loop, args=(len(wallets),), daemon=True)
        hb_thread.start()
        logger.info("[main] Spawned periodic Telegram heartbeat update thread (every 5 minutes).")

    run_number = 0

    while True:
        run_number += 1
        logger.info(f"\n{'#'*60}")
        logger.info(f"[main] === RUN #{run_number} ===")
        logger.info(f"{'#'*60}")

        # Reload config each run — config.json changes take effect without restart.
        # Wallet list and proxies are NOT reloaded (require process restart).
        try:
            bot_cfg = BotConfig()
        except Exception as e:
            logger.error(f"[main] Failed to reload config: {e}. Using previous config.")

        global _CURRENT_STATE, _LAST_RUN_STATS, _NEXT_RUN_TIME
        _CURRENT_STATE = "Running Pass"

        stats = run_once(
            bot_cfg=bot_cfg,
            wallets=wallets,
            proxies=proxies,
            dry_run=args.dry_run,
            force_run=args.force,
            interval_hours=args.interval,
            is_loop=args.loop,
        )

        logger.info(
            f"[main] Run #{run_number} complete — "
            f"{stats['total_success']}/{stats['total_attempted']} sessions minted."
        )

        # Update last run stats
        from datetime import datetime
        _LAST_RUN_STATS = {
            "success": stats["total_success"],
            "attempted": stats["total_attempted"],
            "run_number": run_number,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }

        # Send run summary alert (next_run_hours is None for single runs)
        if not args.loop:
            alert_run_summary(run_number, stats["total_success"], stats["total_attempted"])
            break

        # Jitter ±10% — prevents clock-aligned daily tx clusters
        jitter = random.uniform(-0.10, 0.10)
        actual_secs = args.interval * 3600 * (1 + jitter)
        actual_hours = actual_secs / 3600

        logger.info(
            f"[main] Next run in {actual_hours:.1f}h "
            f"(base {args.interval}h, jitter {jitter:+.1%}). Press Ctrl+C to stop."
        )

        # Send run summary alert (includes next run time)
        alert_run_summary(run_number, stats["total_success"], stats["total_attempted"], actual_hours)

        _CURRENT_STATE = "Sleeping"
        _NEXT_RUN_TIME = time.time() + actual_secs

        try:
            time.sleep(actual_secs)
        except KeyboardInterrupt:
            logger.info("[main] Interrupted. Exiting.")
            break


if __name__ == "__main__":
    main()