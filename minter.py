"""
minter.py — Per-wallet minting engine for Sweep Haus.

Changes over original code:
- Gas estimated via estimate_gas() with 20% buffer; config gas_limit as fallback
- Revert detection narrowed: only marks sold_out on EVM contract revert
- Cooldown only set on success (configurable via cooldown_on_fail)
- Mint state keyed by (chain_key, contract) — no cross-chain collisions
- Balance check uses BigInt math, no floats
- receipt.status == 0 caught (mined revert without exception)
- Nonce managed locally per session; re-synced on nonce-too-low error
- Transfer event verified in receipt: confirms NFT landed in correct wallet
- gas_buffer_units renamed from gas_buffer_gwei (it's a unit count, not a price)
- BearerTokenError from sweep_api propagated cleanly — chain is skipped

[RISK] estimate_gas fails if the call would revert. Fallback is config gas_limit.
       The tx may still submit and revert. No universal fix — you need revert reason decoding.
[RISK] gas_limit flat value may underestimate on complex contracts. Monitor failed txns.
"""

import logging
import random
import time
from typing import Optional

from web3 import Web3

import sweep_api
from sweep_api import BearerTokenError
from calldata import build_claim_calldata
from chain_config import ChainConfig

logger = logging.getLogger("minter")

# ERC-721 Transfer event topic (keccak256 of "Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Revert strings that reliably indicate sold-out or eligibility failure.
# NOT general RPC errors, timeouts, or nonce issues.
REVERT_STRINGS = [
    "!qty",
    "exceed",
    "sold out",
    "max supply",
    "!maxsupply",
    "drop not active",
    "not enough supply",
    "claimcondition",   # ThirdWeb ClaimCondition revert
]

TRANSIENT_ERRORS = [
    "timeout",
    "connection",
    "rpc",
    "nonce too low",
    "replacement transaction",
    "already known",
    "insufficient funds",   # wallet issue, not contract
]


def _is_contract_revert(error_str: str) -> bool:
    """True only if the error is an EVM contract-level revert, not a transient failure."""
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
    """On-chain totalSupply check. Returns False if max_supply is unknown (0)."""
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
        total = contract.functions.totalSupply().call()
        return total >= max_supply
    except Exception as e:
        logger.debug(f"[minter] totalSupply() failed for {contract_address}: {e}")
        return False


def _estimate_gas(w3: Web3, tx: dict, fallback: int) -> int:
    """estimate_gas with 20% buffer. Falls back to config gas_limit on failure."""
    try:
        tx_for_estimate = {k: v for k, v in tx.items() if k != "gas"}
        estimated = w3.eth.estimate_gas(tx_for_estimate)
        return int(estimated * 1.2)
    except Exception as e:
        logger.debug(f"[minter] estimate_gas failed ({e}), using config limit {fallback}")
        return fallback


def _verify_transfer_in_receipt(receipt: dict, recipient: str, contract_address: str) -> bool:
    """
    Verify a Transfer event exists in receipt logs where:
      - log.address matches the NFT contract
      - topic[0] == ERC-721 Transfer topic
      - topic[2] (indexed 'to') matches recipient address

    Returns True if confirmed, False if no matching log found.
    A False result after status==1 means the contract did something unusual —
    worth logging but not worth marking sold_out.
    """
    recipient_padded = recipient.lower().replace("0x", "").zfill(64)
    contract_lower = contract_address.lower()

    logs = receipt.get("logs", [])
    for log in logs:
        log_addr = log.get("address", "").lower()
        topics = log.get("topics", [])

        if log_addr != contract_lower:
            continue
        if not topics:
            continue

        # topics[0] is the event signature
        t0 = topics[0]
        if hasattr(t0, "hex"):
            t0 = t0.hex()
        if t0.lower() != TRANSFER_TOPIC:
            continue

        # ERC-721: Transfer(address indexed from, address indexed to, uint256 indexed tokenId)
        # topics[2] is 'to'
        if len(topics) < 3:
            continue
        t2 = topics[2]
        if hasattr(t2, "hex"):
            t2 = t2.hex()
        if recipient_padded in t2.lower().replace("0x", ""):
            return True

    return False


def perform_mint(
    chain: ChainConfig,
    address: str,
    private_key: str,
    cooldown_hours: float,
    cooldown_on_fail: bool,
    target_mints_range: list,
    index_cache_hours: float,
    max_api_pages: int,
    dry_run: bool = False,
    proxy_dict: Optional[dict] = None,
    sweep_fee_wei: int = 202000000000000,
) -> bool:
    """
    Mint 1–N random Sweep Haus NFTs for one wallet on one chain.

    Args:
        dry_run: If True, skips actual tx broadcast. Full pipeline runs (RPC, balance,
                 calldata) but send_raw_transaction is not called. Safe for config testing.

    Returns True if at least one NFT was successfully minted (or dry_run completed).
    Raises BearerTokenError if API auth fails — caller should skip the chain.
    """
    tag = f"[{address[:8]}][{chain.chain_key}]"
    if dry_run:
        tag = f"[DRY-RUN]{tag}"

    # ── 1. Cooldown gate ──────────────────────────────────────────────
    if sweep_api.is_on_cooldown(address, cooldown_hours):
        return False

    # ── 2. RPC connection ─────────────────────────────────────────────
    w3 = chain.get_w3()
    if not w3:
        logger.error(f"{tag} Could not connect to any RPC. Skipping.")
        return False

    # ── 3. Get collections (may raise BearerTokenError) ──────────────
    collections = sweep_api.get_active_collections(
        chain.chain_key,
        chain.sweep_haus_chain_id,
        chain.max_price_native,
        index_cache_hours,
        max_api_pages,
        proxy_dict,
    )

    if not collections:
        logger.info(f"{tag} No active collections found.")
        return False

    # Filter out already-minted contracts for this wallet+chain
    wallet_record = sweep_api.get_wallet_record(address)
    minted_on_chain = set(
        wallet_record.get("minted_contracts", {}).get(chain.chain_key, [])
    )
    candidates = [c for c in collections if c["contract"].lower() not in minted_on_chain]

    if not candidates:
        logger.info(f"{tag} All indexed collections already minted on this chain.")
        return False

    # ── 4. Determine session target ───────────────────────────────────
    target = random.randint(target_mints_range[0], target_mints_range[1])
    logger.info(f"{tag} Targeting {target} mints from {len(candidates)} candidates.")
    random.shuffle(candidates)

    # ── 5. Gas price ──────────────────────────────────────────────────
    if chain.gas_price_gwei is not None:
        base_gas_price = w3.to_wei(chain.gas_price_gwei, "gwei")
    else:
        base_gas_price = w3.eth.gas_price

    max_fee = int(base_gas_price * chain.gas_multiplier)
    priority_fee = int(base_gas_price * chain.priority_multiplier)

    # ── 6. Nonce (managed locally; re-synced on nonce-too-low) ────────
    nonce = w3.eth.get_transaction_count(address, "pending")

    success_count = 0

    for pick in candidates:
        if success_count >= target:
            break

        contract_address = pick["contract"]
        name = pick.get("name", "Unknown")
        price = pick.get("price", 0.0)
        max_supply = pick.get("max_supply", 0)

        # On-chain sold-out check
        if _check_sold_out(w3, contract_address, max_supply):
            logger.info(f"{tag} '{name}' sold out on-chain. Marking.")
            sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
            continue

        # Balance check — all BigInt, no floats
        price_wei = int(price * 10**18)
        total_value_wei = sweep_fee_wei + price_wei
        # gas_buffer_units * base_gas_price = estimated max gas cost in wei
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
            break  # Balance won't recover mid-session

        # Build tx
        calldata = build_claim_calldata(
            recipient=address,
            sweep_fee_wei=sweep_fee_wei,
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

        # ── Dry run — stop before broadcast ──────────────────────────
        if dry_run:
            logger.info(
                f"{tag} Would mint '{name}' | price={price} {chain.native_symbol} | "
                f"gas={tx['gas']} | nonce={nonce} | value={w3.from_wei(total_value_wei, 'ether')}"
            )
            success_count += 1
            nonce += 1
            time.sleep(0.2)
            continue

        # ── Live tx ───────────────────────────────────────────────────
        try:
            logger.info(
                f"{tag} Minting '{name}' | price={price} {chain.native_symbol} | nonce={nonce}"
            )
            signed = w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            tx_hex = tx_hash.hex()

            if receipt.get("status") == 0:
                logger.warning(f"{tag} TX reverted: {tx_hex} | '{name}'")
                if _check_sold_out(w3, contract_address, max_supply):
                    sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
                nonce += 1
                time.sleep(2)
                continue

            # Verify Transfer event landed in correct wallet
            transfer_confirmed = _verify_transfer_in_receipt(receipt, address, contract_address)
            if not transfer_confirmed:
                logger.warning(
                    f"{tag} TX succeeded (status=1) but no Transfer to {address[:8]} "
                    f"found in logs. Recording anyway — inspect manually: {tx_hex}"
                )

            logger.info(f"{tag} ✓ Minted '{name}' | TX: {tx_hex}")
            sweep_api.record_mint(address, chain.chain_key, contract_address)
            success_count += 1
            nonce += 1
            time.sleep(random.uniform(2, 5))

        except Exception as e:
            err_str = str(e)
            logger.warning(f"{tag} Failed to mint '{name}': {err_str}")

            if _is_contract_revert(err_str):
                logger.info(f"{tag} Contract revert — marking '{name}' sold_out.")
                sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
                nonce += 1
            elif "nonce too low" in err_str.lower():
                nonce = w3.eth.get_transaction_count(address, "pending")
            # Other errors: don't increment nonce (tx may not have broadcast)

            time.sleep(2)

    # ── 7. Cooldown bookkeeping ───────────────────────────────────────
    if success_count > 0 or cooldown_on_fail:
        if not dry_run:
            sweep_api.set_last_run(address)

    verb = "Simulated" if dry_run else "Minted"
    logger.info(f"{tag} Session done. {verb} {success_count}/{target} NFTs.")
    return success_count > 0
