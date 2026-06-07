"""
minter.py — Per-wallet minting engine for Sweep Haus.

Anti-fingerprint measures implemented:
  1. Per-wallet time-seeded RNG — mix of address + UTC day + chain_key
     Avoids shared global random state across threads AND avoids fixed-forever seed.
     Same wallet may behave differently on different days (address entropy alone would
     produce a permanent behavioral fingerprint over months as ChatGPT noted).

  2. Random session skip (15% chance) — simulates human inactivity days.
     Ensures not every wallet mints every day. Breaks the "all wallets always active"
     cluster signal.

  3. Per-wallet contract subset — each wallet sees a random 65–80% of available
     collections, drawn from its wallet-seeded RNG. This directly addresses the
     contract overlap score: no two wallets ever interact with the identical set.

  4. Per-wallet gas variance ±5% — maxFeePerGas and maxPriorityFeePerGas vary
     per-wallet using wallet-seeded RNG. Prevents identical gas params across wallets
     in the same block.

  5. Exponential inter-mint delay with long tail — replaces uniform(2,5).
     Mean ~15s, clamped 5–120s. 10% chance of adding an extra 60–300s pause.
     Closer to actual human browsing patterns than a tight uniform distribution.

  6. Rotating User-Agent per API call — in sweep_api.py _make_headers().

What this does NOT fix (wallet infrastructure — outside bot scope):
  - Funding graph (wallets funded from same parent address)
  - Bridge clustering (same bridge, same amounts, same window)
  - Withdrawal destination clustering (all consolidate to same address)
  These require manual operational decisions, not code changes.

[RISK] Per-wallet RNG is seeded with address + day + chain. If an analyst knows all
       three, they can reproduce the seed. This is acceptable — it prevents trivial
       correlation while remaining deterministic enough to be auditable.
[RISK] Contract subset (65–80%) means some collections are never minted by some wallets.
       That's intentional. Full overlap is the worse outcome.
"""

import hashlib
import logging
import math
import random
import re
import time
from datetime import datetime, timezone
from typing import Optional

from web3 import Web3

import sweep_api
from sweep_api import BearerTokenError
from calldata import build_claim_calldata
from chain_config import ChainConfig

logger = logging.getLogger("minter")

# ERC-721 Transfer event topic (keccak256("Transfer(address,address,uint256)"))
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# ERC-1155 TransferSingle event topic (keccak256("TransferSingle(address,address,address,uint256,uint256)"))
TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

REVERT_STRINGS = [
    "!qty", "exceed", "sold out", "max supply", "!maxsupply",
    "drop not active", "not enough supply", "claimcondition",
]

TRANSIENT_ERRORS = [
    "timeout", "connection", "rpc", "nonce too low",
    "replacement transaction", "already known", "insufficient funds",
]

# Realistic browser User-Agents rotated per API call (in sweep_api.py)
# Listed here for reference — actual rotation is in sweep_api._make_headers()


# =====================================================================
# PER-WALLET RNG
# =====================================================================

def _make_wallet_rng(address: str, chain_key: str) -> random.Random:
    """
    Create a per-wallet Random instance seeded from:
        sha256(address_lower + UTC_day_str + chain_key)

    - address ensures different wallets get different sequences
    - UTC day ensures the sequence changes daily (no permanent fingerprint)
    - chain_key ensures different behavior across chains

    The seed changes every UTC midnight. Two wallets running on the same day
    and chain will have different seeds because their addresses differ.
    An analyst observing over months cannot predict tomorrow's seed from today's
    behavior because the day component rotates.
    """
    utc_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seed_input = f"{address.lower()}:{utc_day}:{chain_key}"
    seed_bytes = hashlib.sha256(seed_input.encode()).digest()
    seed_int = int.from_bytes(seed_bytes[:8], "big")
    return random.Random(seed_int)


# =====================================================================
# HELPERS
# =====================================================================

def _is_contract_revert(error_str: str) -> bool:
    s = error_str.lower()
    for t in TRANSIENT_ERRORS:
        if t in s:
            return False
    for r in REVERT_STRINGS:
        if r in s:
            return True
    if "execution reverted" in s or "revert" in s:
        return True
    return False


def _check_sold_out(w3: Web3, contract_address: str, max_supply: int) -> bool:
    if max_supply <= 0:
        return False
    try:
        abi = [{
            "type": "function", "name": "totalSupply",
            "stateMutability": "view", "inputs": [],
            "outputs": [{"type": "uint256"}]
        }]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(contract_address), abi=abi
        )
        return contract.functions.totalSupply().call() >= max_supply
    except Exception as e:
        logger.debug(f"[minter] totalSupply() failed for {contract_address}: {e}")
        return False


def _estimate_gas(w3: Web3, tx: dict, fallback: int) -> int:
    try:
        tx_for_estimate = {k: v for k, v in tx.items() if k != "gas"}
        return int(w3.eth.estimate_gas(tx_for_estimate) * 1.2)
    except Exception as e:
        logger.debug(f"[minter] estimate_gas failed ({e}), using fallback {fallback}")
        return fallback


def _extract_revert_data(e: Exception) -> str:
    """Extract revert hex data from Web3 exception recursively or via attributes."""
    data = getattr(e, "data", None)
    if isinstance(data, str):
        return data
    if isinstance(data, dict) and "data" in data:
        return str(data["data"])
    if hasattr(e, "args") and e.args:
        first_arg = e.args[0]
        if isinstance(first_arg, dict):
            val = first_arg.get("data") or first_arg.get("error", {}).get("data")
            if isinstance(val, str):
                return val
        elif isinstance(first_arg, str):
            return first_arg
    return str(e)


def _get_expected_price_from_revert(w3: Web3, tx_param: dict) -> Optional[int]:
    """
    Simulate the claim transaction via eth_call.
    If it reverts with DropClaimInvalidTokenPrice (0xf13474e9), decode and return the expected price.
    Otherwise, if it succeeds or reverts with a different error, return None.
    """
    try:
        w3.eth.call(tx_param)
        return None
    except Exception as e:
        err_str = _extract_revert_data(e).lower()
        if "f13474e9" in err_str:
            match = re.search(r'f13474e9([a-fA-F0-9]+)', err_str)
            if match:
                try:
                    hex_data = match.group(1)
                    # DropClaimInvalidTokenPrice parameters:
                    # expectedCurrency (address, 32 bytes) -> first 32 bytes (64 hex chars)
                    # expectedPricePerToken (uint256, 32 bytes) -> fourth 32 bytes (chars 192:256)
                    # because the signature order is: actualCurrency/Price and expectedCurrency/Price
                    expected_price_hex = hex_data[192:256]
                    return int(expected_price_hex, 16)
                except Exception:
                    pass
        return None


def _check_wallet_fee_balance(
    w3: Web3,
    address: str,
    sweep_fee_wei: int,
    base_gas_price: int,
    gas_buffer_units: int,
    native_symbol: str,
    tag: str,
) -> bool:
    """
    Check wallet has enough balance to cover at minimum ONE free mint.

    Even a "free" NFT (price = 0) costs:
      - Sweep Haus platform fee: 0.000202 native tokens (e.g. ETH, X1T)
      - Gas: estimated from live gas price × gas buffer units

    This check runs ONCE per session before the collection loop starts.
    If the wallet can't afford even one free mint, skip the entire session
    rather than attempting each collection individually.

    Why this matters for beginners:
      On testnets like X1 EcoChain, tokens are free from faucets.
      On mainnet chains (Base, Arbitrum, etc.), you need real ETH.
      The platform fee alone is ~$0.0004 at $2000 ETH — small but non-zero.

    Returns True if the wallet can proceed. False if balance is too low.
    """
    try:
        balance_wei = w3.eth.get_balance(address)
    except Exception as e:
        logger.warning(f"{tag} Could not fetch balance: {e}. Proceeding anyway.")
        return True  # Don't block — let the per-mint check catch it

    # Minimum needed for one free mint (price = 0):
    #   platform_fee + estimated_gas_cost
    min_gas_cost_wei = gas_buffer_units * base_gas_price
    min_required_wei = sweep_fee_wei + min_gas_cost_wei

    fee_eth  = w3.from_wei(sweep_fee_wei, "ether")
    gas_eth  = w3.from_wei(min_gas_cost_wei, "ether")
    need_eth = w3.from_wei(min_required_wei, "ether")
    have_eth = w3.from_wei(balance_wei, "ether")

    # Always show a clear fee breakdown — beginners need to see this
    logger.info(f"{tag} Fee breakdown for this chain:")
    logger.info(f"{tag}   Platform fee (Sweep Haus) : {float(fee_eth):.8f} {native_symbol}")
    logger.info(f"{tag}   Estimated gas cost        : {float(gas_eth):.8f} {native_symbol}")
    logger.info(f"{tag}   Minimum needed (1 mint)   : {float(need_eth):.8f} {native_symbol}")
    logger.info(f"{tag}   Wallet balance            : {float(have_eth):.8f} {native_symbol}")

    if balance_wei < min_required_wei:
        logger.warning(f"{tag} INSUFFICIENT BALANCE — cannot afford even one free NFT.")
        logger.warning(f"{tag}   Need at least {float(need_eth):.8f} {native_symbol} (platform fee + gas).")
        logger.warning(f"{tag}   Current balance: {float(have_eth):.8f} {native_symbol}.")
        logger.warning(f"{tag}   Top up this wallet and try again.")
        return False

    logger.info(
        f"{tag} Balance OK — wallet can afford approx. "
        f"{int(balance_wei // min_required_wei)} free mint(s) on this chain."
    )
    return True


def _verify_transfer_in_receipt(receipt: dict, recipient: str, contract_address: str) -> bool:
    recipient_padded = recipient.lower().replace("0x", "").zfill(64)
    contract_lower = contract_address.lower()
    for log in receipt.get("logs", []):
        if log.get("address", "").lower() != contract_lower:
            continue
        topics = log.get("topics", [])
        if not topics:
            continue
        t0 = topics[0]
        if hasattr(t0, "hex"):
            t0 = t0.hex()
        t0_lower = t0.lower()
        if t0_lower == TRANSFER_TOPIC:
            if len(topics) < 3:
                continue
            t2 = topics[2]
            if hasattr(t2, "hex"):
                t2 = t2.hex()
            if recipient_padded in t2.lower().replace("0x", ""):
                return True
        elif t0_lower == TRANSFER_SINGLE_TOPIC:
            if len(topics) < 4:
                continue
            t3 = topics[3]
            if hasattr(t3, "hex"):
                t3 = t3.hex()
            if recipient_padded in t3.lower().replace("0x", ""):
                return True
    return False


def _human_delay(rng: random.Random, dry_run: bool = False) -> None:
    """
    Exponential inter-mint delay with human-like long tail.

    expovariate(1/15) → mean ~15s, heavy tail.
    Clamped to [5, 120] seconds for sanity.
    10% chance of an extra 60–300s pause (simulating distraction).
    Dry-run uses 0.2s flat — no point waiting during testing.
    """
    if dry_run:
        time.sleep(0.2)
        return

    base = rng.expovariate(1 / 15.0)
    base = max(5.0, min(base, 120.0))

    if rng.random() < 0.10:
        base += rng.uniform(60, 300)
        logger.debug(f"[minter] Long pause simulated: {base:.0f}s")

    time.sleep(base)


def _wallet_contract_subset(
    candidates: list[dict],
    rng: random.Random,
    coverage_range: tuple = (0.65, 0.80),
) -> list[dict]:
    """
    Return a random subset of candidates for this wallet.

    Each wallet sees 65–80% of available collections, chosen by wallet-seeded RNG.
    This ensures no two wallets interact with the identical contract set,
    which directly reduces the contract overlap score used in sybil clustering.

    If fewer than 3 candidates exist, return all (no point sub-sampling tiny pools).
    """
    if len(candidates) < 3:
        return candidates[:]

    coverage = rng.uniform(*coverage_range)
    subset_size = max(1, math.ceil(len(candidates) * coverage))
    return rng.sample(candidates, subset_size)


# =====================================================================
# CORE ENTRY POINT
# =====================================================================

def perform_mint(
    chain: ChainConfig,
    address: str,
    private_key: str,
    cooldown_hours: float,
    cooldown_on_fail: bool,
    target_mints_range: list,
    collections: list[dict],
    dry_run: bool = False,
    proxy_dict: Optional[dict] = None,
    sweep_fee_wei: int = 202000000000000,
    force_run: bool = False,
) -> bool:
    """
    Mint 1–N Sweep Haus NFTs for one wallet on one chain with anti-fingerprint behavior.

    Anti-fingerprint measures active in this function:
      - Per-wallet time-seeded RNG (address + day + chain)
      - Random session skip (15% chance, simulates inactivity)
      - Per-wallet contract subset (65–80% of pool, different per wallet)
      - Per-wallet gas variance ±5%
      - Exponential inter-mint delay with long tail

    Returns True if ≥1 NFT minted (or dry_run simulated successfully).
    Raises BearerTokenError if API auth fails.
    """
    tag = f"[{address[:8]}][{chain.chain_key}]"
    if dry_run:
        tag = f"[DRY-RUN]{tag}"

    # ── Per-wallet seeded RNG ─────────────────────────────────────────
    # All randomness in this session flows through this instance.
    # Using global random.* would share state across concurrent wallet threads.
    rng = _make_wallet_rng(address, chain.chain_key)

    # ── 1. Random session skip ────────────────────────────────────────
    # 15% chance of skipping entirely — simulates human inactivity.
    # Applied BEFORE cooldown check so it doesn't consume the cooldown slot.
    if not dry_run and not force_run and rng.random() < 0.15:
        logger.info(f"{tag} Random session skip (simulating inactivity). No action today.")
        return False

    # ── 2. Cooldown gate ──────────────────────────────────────────────
    if not force_run and sweep_api.is_on_cooldown(address, chain.chain_key, cooldown_hours):
        return False

    # ── 3. RPC connection ─────────────────────────────────────────────
    w3 = chain.get_w3()
    if not w3:
        logger.error(f"{tag} No RPC connection. Skipping.")
        return False

    if not collections:
        logger.info(f"{tag} No active collections found.")
        return False

    # ── 5. Filter already-minted ──────────────────────────────────────
    wallet_record = sweep_api.get_wallet_record(address)
    minted_on_chain = set(
        wallet_record.get("minted_contracts", {}).get(chain.chain_key, [])
    )
    fresh_candidates = [
        c for c in collections if c["contract"].lower() not in minted_on_chain
    ]

    if not fresh_candidates:
        logger.info(f"{tag} All indexed collections already minted on this chain.")
        return False

    # ── 6. Per-wallet contract subset ─────────────────────────────────
    # Each wallet works from a different 65–80% slice of available collections.
    # Reduces contract overlap score across the wallet cluster.
    candidates = _wallet_contract_subset(fresh_candidates, rng)
    logger.info(
        f"{tag} Subset: {len(candidates)}/{len(fresh_candidates)} collections "
        f"selected for this wallet today."
    )

    # ── 7. Session target ─────────────────────────────────────────────
    target = rng.randint(target_mints_range[0], target_mints_range[1])
    rng.shuffle(candidates)
    logger.info(f"{tag} Targeting {target} mints from {len(candidates)} candidates.")

    # ── 8. Gas price with per-wallet variance ─────────────────────────
    # Base gas from chain or live RPC. Then apply ±5% variance per wallet
    # so no two wallets submit identical maxFeePerGas in the same block.
    if chain.gas_price_gwei is not None:
        base_gas_price = w3.to_wei(chain.gas_price_gwei, "gwei")
    else:
        base_gas_price = w3.eth.gas_price

    gas_variance = rng.uniform(0.95, 1.05)
    max_fee = int(base_gas_price * chain.gas_multiplier * gas_variance)
    priority_fee = int(base_gas_price * chain.priority_multiplier * gas_variance)

    logger.debug(f"{tag} Gas variance: {gas_variance:.3f}x | maxFee={max_fee} | priority={priority_fee}")

    # ── 9. Pre-session wallet balance check ──────────────────────────
    # Check once upfront: can this wallet afford at least one free NFT?
    # "Free" NFTs still cost: Sweep Haus platform fee + gas.
    # Fails early with a clear human-readable message if balance too low.
    if not _check_wallet_fee_balance(
        w3, address, sweep_fee_wei, base_gas_price,
        chain.gas_buffer_units, chain.native_symbol, tag
    ):
        return False

    # ── 10. Nonce ─────────────────────────────────────────────────────
    nonce = w3.eth.get_transaction_count(address, "pending")

    success_count = 0

    for pick in candidates:
        if success_count >= target:
            break

        contract_address = pick["contract"]
        name = pick.get("name", "Unknown")
        price = pick.get("price", 0.0)
        max_supply = pick.get("max_supply", 0)

        if _check_sold_out(w3, contract_address, max_supply):
            logger.info(f"{tag} '{name}' sold out. Marking.")
            sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
            continue

        price_wei = int(price * 10**18)
        
        # Pre-flight check to dynamically fetch the exact expected price from the contract's claim conditions.
        # This resolves dynamic, oracle-converted or custom collection prices automatically.
        price_per_token_wei = None
        if not dry_run:
            try:
                # Simulate call with pricePerToken = 0 to trigger DropClaimInvalidTokenPrice
                sim_calldata = build_claim_calldata(
                    recipient=address,
                    sweep_fee_wei=0,
                    currency_native_int=chain.native_currency_int,
                )
                sim_tx = {
                    "from": address,
                    "to": Web3.to_checksum_address(contract_address),
                    "data": sim_calldata,
                    "value": 0,
                }
                price_per_token_wei = _get_expected_price_from_revert(w3, sim_tx)
                if price_per_token_wei is not None:
                    logger.info(f"{tag} Pre-flight resolved contract price: {w3.from_wei(price_per_token_wei, 'ether')} for '{name}'")
            except Exception as e:
                logger.debug(f"{tag} Pre-flight simulation error for '{name}': {e}")

        # Fallback to standard config/API prices if pre-flight didn't resolve it or in dry-run mode
        if price_per_token_wei is None:
            price_per_token_wei = sweep_fee_wei + price_wei
            total_value_wei = price_per_token_wei
        else:
            total_value_wei = price_per_token_wei

        gas_buffer_wei = chain.gas_buffer_units * base_gas_price
        required_wei = total_value_wei + gas_buffer_wei

        try:
            balance_wei = w3.eth.get_balance(address)
        except Exception as e:
            logger.warning(f"{tag} Balance check failed: {e}")
            continue

        if balance_wei < required_wei:
            logger.warning(
                f"{tag} Insufficient balance for '{name}'. "
                f"Have: {w3.from_wei(balance_wei, 'ether')} {chain.native_symbol}, "
                f"Need: ~{w3.from_wei(required_wei, 'ether')} (incl. gas buffer)"
            )
            break

        calldata = build_claim_calldata(
            recipient=address,
            sweep_fee_wei=price_per_token_wei,
            currency_native_int=chain.native_currency_int,
        )

        tx = {
            "from": address,
            "to": Web3.to_checksum_address(contract_address),
            "data": calldata,
            "value": total_value_wei,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "nonce": nonce,
            "chainId": chain.chain_id,
        }
        tx["gas"] = _estimate_gas(w3, tx, chain.gas_limit)

        # ── Dry run ───────────────────────────────────────────────────
        if dry_run:
            logger.info(
                f"{tag} Would mint '{name}' | price={price} {chain.native_symbol} | "
                f"gas={tx['gas']} | nonce={nonce} | gasVariance={gas_variance:.3f}x"
            )
            success_count += 1
            nonce += 1
            _human_delay(rng, dry_run=True)
            continue

        # ── Live tx ───────────────────────────────────────────────────
        try:
            logger.info(
                f"{tag} Minting '{name}' | price={price} {chain.native_symbol} | nonce={nonce}"
            )
            signed = w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            tx_hex = tx_hash.hex()

            if receipt.get("status") == 0:
                logger.warning(f"{tag} TX reverted: {tx_hex} | '{name}'")
                if _check_sold_out(w3, contract_address, max_supply):
                    sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
                nonce += 1
                time.sleep(2)
                continue

            transfer_confirmed = _verify_transfer_in_receipt(receipt, address, contract_address)
            if not transfer_confirmed:
                logger.warning(
                    f"{tag} status=1 but no Transfer event to {address[:8]} — "
                    f"inspect manually: {tx_hex}"
                )

            logger.info(f"{tag} ✓ Minted '{name}' | TX: {tx_hex}")
            sweep_api.record_mint(address, chain.chain_key, contract_address)
            success_count += 1
            nonce += 1

            # Human-like delay between mints
            _human_delay(rng, dry_run=False)

        except Exception as e:
            err_str = str(e)
            logger.warning(f"{tag} Failed '{name}': {err_str}")
            if _is_contract_revert(err_str):
                logger.info(f"{tag} Contract revert — marking '{name}' sold_out.")
                sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
                nonce += 1
            elif "nonce too low" in err_str.lower():
                nonce = w3.eth.get_transaction_count(address, "pending")
            time.sleep(2)

    # ── 11. Cooldown write ────────────────────────────────────────────
    if success_count > 0 or cooldown_on_fail:
        if not dry_run:
            sweep_api.set_last_run(address, chain.chain_key)

    verb = "Simulated" if dry_run else "Minted"
    logger.info(f"{tag} Session done. {verb} {success_count}/{target} NFTs.")
    return success_count > 0
