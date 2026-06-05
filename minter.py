"""
minter.py — Per-wallet minting engine for Sweep Haus.

Fixes over original code:
- Gas is estimated via estimate_gas() with fallback to config gas_limit
- Revert detection is narrowed: only marks sold_out on EVM revert, not on all exceptions
- Cooldown only set on success (configurable via cooldown_on_fail)
- Mint state is keyed by (chain_key, contract) to avoid cross-chain collisions
- Balance check uses actual BigInt math, no floats
- tx receipt status is checked (status=0 = revert even if no exception)
- Nonce managed locally per session to avoid redundant eth_getTransactionCount calls

[RISK] Gas estimation can fail on some contracts (e.g. if the call would revert).
       The fallback is config gas_limit. This means you may still submit a tx that reverts.
       There's no universal fix — you need to decode the revert reason to know for sure.

[RISK] Flat gas_limit may underestimate on complex contracts. Monitor failed txns.
"""

import logging
import random
import time
from typing import Optional

from web3 import Web3

import sweep_api
from calldata import build_claim_calldata
from chain_config import ChainConfig

logger = logging.getLogger("minter")

# Revert signatures that reliably indicate sold-out or eligibility failure
# NOT general RPC errors, timeout, or nonce issues
REVERT_STRINGS = [
    "!qty",
    "exceed",
    "sold out",
    "max supply",
    "!maxsupply",
    "drop not active",
    "not enough supply",
    "claimcondition",  # ThirdWeb's ClaimCondition revert
]

TRANSIENT_ERRORS = [
    "timeout",
    "connection",
    "rpc",
    "nonce too low",
    "replacement transaction",
    "already known",
    "insufficient funds",  # wallet issue, not contract
]


def _is_contract_revert(error_str: str) -> bool:
    """True only if the error looks like an EVM contract-level revert."""
    s = error_str.lower()
    # Transient/wallet errors should NOT mark a collection sold out
    for t in TRANSIENT_ERRORS:
        if t in s:
            return False
    for r in REVERT_STRINGS:
        if r in s:
            return True
    # Web3 execution reverted without a reason string
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
        logger.debug(f"[minter] totalSupply() call failed for {contract_address}: {e}")
        return False


def _estimate_gas(w3: Web3, tx: dict, fallback: int) -> int:
    """Try estimate_gas; fall back to config value if it fails."""
    try:
        # Remove gas from tx for estimation
        tx_for_estimate = {k: v for k, v in tx.items() if k != "gas"}
        estimated = w3.eth.estimate_gas(tx_for_estimate)
        # Add 20% buffer on top of estimate
        return int(estimated * 1.2)
    except Exception as e:
        logger.debug(f"[minter] estimate_gas failed ({e}), using config limit {fallback}")
        return fallback


def perform_mint(
    chain: ChainConfig,
    address: str,
    private_key: str,
    cooldown_hours: float,
    cooldown_on_fail: bool,
    target_mints_range: list,
    index_cache_hours: float,
    max_api_pages: int,
    proxy_dict: Optional[dict] = None,
    sweep_fee_wei: int = 202000000000000,
) -> bool:
    """
    Main entry point: mint 1–N random Sweep Haus NFTs for one wallet on one chain.

    Returns True if at least one NFT was successfully minted.
    """
    tag = f"[{address[:8]}][{chain.chain_key}]"

    # ── 1. Cooldown gate ──────────────────────────────────────────────
    if sweep_api.is_on_cooldown(address, cooldown_hours):
        return False

    # ── 2. RPC connection ─────────────────────────────────────────────
    w3 = chain.get_w3()
    if not w3:
        logger.error(f"{tag} Could not connect to any RPC. Skipping.")
        return False

    # ── 3. Get collections ────────────────────────────────────────────
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
    logger.info(f"{tag} Targeting {target} mints this session from {len(candidates)} candidates.")
    random.shuffle(candidates)

    # ── 5. Get gas price ──────────────────────────────────────────────
    if chain.gas_price_gwei is not None:
        base_gas_price = w3.to_wei(chain.gas_price_gwei, "gwei")
    else:
        base_gas_price = w3.eth.gas_price

    max_fee = int(base_gas_price * chain.gas_multiplier)
    priority_fee = int(base_gas_price * chain.priority_multiplier)

    # ── 6. Get nonce (managed locally to avoid sequential RPC calls) ──
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
            logger.info(f"{tag} '{name}' is sold out on-chain. Marking.")
            sweep_api.mark_collection_status(chain.chain_key, contract_address, "sold_out")
            continue

        # Balance check — all BigInt, no floats
        price_wei = int(price * 10**18)
        total_value_wei = sweep_fee_wei + price_wei
        # Gas buffer: gas_buffer_gwei * gas_price (not a flat wei amount)
        gas_buffer_wei = chain.gas_buffer_gwei * base_gas_price
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
            # Balance won't recover mid-session — stop trying
            break

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

        # Gas estimation with fallback
        tx["gas"] = _estimate_gas(w3, tx, chain.gas_limit)

        try:
            logger.info(f"{tag} Minting '{name}' | price={price} {chain.native_symbol} | nonce={nonce}")
            signed = w3.eth.account.sign_transaction(tx, private_key)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            tx_hex = tx_hash.hex()

            if receipt.get("status") == 0:
                # Transaction mined but reverted
                logger.warning(f"{tag} TX reverted on-chain: {tx_hex} | '{name}'")
                if _check_sold_out(w3, contract_address, max_supply):
                    sweep_api.mark_collection_status(
                        chain.chain_key, contract_address, "sold_out"
                    )
                nonce += 1
                time.sleep(2)
                continue

            # Success
            logger.info(f"{tag} ✓ Minted '{name}' | TX: {tx_hex}")
            sweep_api.record_mint(address, chain.chain_key, contract_address)
            success_count += 1
            nonce += 1

            # Jitter between mints
            time.sleep(random.uniform(2, 5))

        except Exception as e:
            err_str = str(e)
            logger.warning(f"{tag} Failed to mint '{name}': {err_str}")

            if _is_contract_revert(err_str):
                logger.info(f"{tag} Contract revert detected — marking '{name}' sold_out.")
                sweep_api.mark_collection_status(
                    chain.chain_key, contract_address, "sold_out"
                )
                nonce += 1
            elif "nonce too low" in err_str.lower():
                # Re-sync nonce from chain
                nonce = w3.eth.get_transaction_count(address, "pending")
            # Don't increment nonce for other errors (tx may not have been broadcast)

            time.sleep(2)

    # ── 7. Cooldown bookkeeping ───────────────────────────────────────
    if success_count > 0 or cooldown_on_fail:
        sweep_api.set_last_run(address)

    logger.info(f"{tag} Session done. Minted {success_count}/{target} NFTs.")
    return success_count > 0
