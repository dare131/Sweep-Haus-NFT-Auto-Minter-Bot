"""
notify.py — Lightweight Telegram alerting for unattended loop mode.

Sends alerts when the bot needs human attention:
  - Bearer token expired (401) — bot cannot refresh collections
  - All RPCs dead for a chain — chain skipped silently otherwise
  - Run summary (optional) — daily digest of mint counts

Setup:
  1. Create a Telegram bot via @BotFather → get BOT_TOKEN
  2. Send any message to your bot, then visit:
     https://api.telegram.org/bot<BOT_TOKEN>/getUpdates
     to get your CHAT_ID
  3. Add to .env:
     TELEGRAM_BOT_TOKEN=123456:ABC-your-token
     TELEGRAM_CHAT_ID=987654321

If TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID are not set, all notify calls
are silent no-ops — alerting is fully optional.

[RISK] Telegram API calls are best-effort. If the call fails (network, rate limit),
       the error is logged but the bot continues. Alerts are never blocking.
[RISK] Messages are sent via HTTPS to Telegram servers. Do not include private keys
       or sensitive wallet data in alert messages — this module never does.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("notify")

_BOT_TOKEN: Optional[str] = None
_CHAT_ID: Optional[str] = None
_ENABLED: bool = False

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def init_notifications() -> bool:
    """
    Load Telegram credentials from environment.
    Returns True if alerting is enabled, False if not configured (silent no-op mode).
    Call once at startup after load_dotenv().
    """
    global _BOT_TOKEN, _CHAT_ID, _ENABLED

    _BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    _CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if _BOT_TOKEN and _CHAT_ID:
        _ENABLED = True
        logger.info("[notify] Telegram alerting enabled.")
    else:
        _ENABLED = False
        logger.info("[notify] Telegram not configured — alerts disabled. (Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to enable)")

    return _ENABLED


def _send(message: str) -> None:
    """
    Fire-and-forget Telegram message. Never raises — failures are logged only.
    All public functions in this module call this internally.
    """
    if not _ENABLED:
        return

    try:
        url = TELEGRAM_API.format(token=_BOT_TOKEN)
        resp = requests.post(
            url,
            json={
                "chat_id": _CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if not resp.ok:
            logger.warning(f"[notify] Telegram API error: {resp.status_code} {resp.text[:100]}")
    except Exception as e:
        logger.warning(f"[notify] Failed to send Telegram alert: {e}")


# =====================================================================
# PUBLIC ALERT FUNCTIONS
# =====================================================================

def alert_bearer_expired(chain_key: str) -> None:
    """
    Called when the API returns 401. Requires immediate human action.
    Bot will keep sleeping and retrying until token is updated.
    """
    _send(
        f"🔴 <b>Sweep Haus Bot — Bearer Token Expired</b>\n\n"
        f"Chain: <code>{chain_key}</code>\n"
        f"The API returned 401. Collections cannot be refreshed.\n\n"
        f"<b>Action required:</b> Get a new bearer token from sweep.haus "
        f"(DevTools → Network) and update <code>BEARER</code> in your .env, "
        f"then restart the bot."
    )


def alert_rpc_down(chain_key: str, rpc_urls: list[str]) -> None:
    """Called when all RPC endpoints for a chain fail to connect."""
    urls_str = "\n".join(f"  • {u}" for u in rpc_urls)
    _send(
        f"🟡 <b>Sweep Haus Bot — RPC Down</b>\n\n"
        f"Chain: <code>{chain_key}</code>\n"
        f"All RPCs failed:\n{urls_str}\n\n"
        f"Chain skipped this run. Add a working RPC to <code>rpc.json</code>."
    )


def alert_run_summary(run_number: int, success: int, attempted: int, next_run_hours: Optional[float] = None) -> None:
    """Optional digest. Called at end of each run."""
    emoji = "✅" if success > 0 else "⚪"
    msg = (
        f"{emoji} <b>Sweep Haus Bot — Run #{run_number} Complete</b>\n\n"
        f"Minted: <b>{success}</b> / {attempted} sessions"
    )
    if next_run_hours is not None and next_run_hours > 0:
        msg += f"\nNext run in: ~{next_run_hours:.1f}h"
    _send(msg)


def alert_startup(mode: str, chains: list[str], wallet_count: int, dry_run: bool) -> None:
    """Sent once when the bot starts in loop mode — confirms it's running."""
    mode_str = "DRY-RUN" if dry_run else "LIVE"
    chains_str = ", ".join(f"<code>{c}</code>" for c in chains)
    _send(
        f"🟢 <b>Sweep Haus Bot Started</b> [{mode_str}]\n\n"
        f"Mode: <code>{mode}</code>\n"
        f"Chains: {chains_str}\n"
        f"Wallets: {wallet_count}"
    )


def alert_heartbeat(
    status: str,
    uptime_str: str,
    wallet_count: int,
    last_run_summary: str,
    next_run_summary: str,
) -> None:
    """Sends a periodic status update to confirm the bot is alive and show progress."""
    _send(
        f"💓 <b>Sweep Haus Bot Status</b>\n\n"
        f"Status: <code>{status}</code>\n"
        f"Uptime: <code>{uptime_str}</code>\n"
        f"Wallets: <code>{wallet_count}</code>\n\n"
        f"Last Run: <code>{last_run_summary}</code>\n"
        f"Next Run: <code>{next_run_summary}</code>"
    )
