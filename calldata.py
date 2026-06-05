"""
calldata.py — Raw calldata builders for Sweep Haus claim() transactions.

The claim() selector is 0x57bc3d78 and encodes:
  (address recipient, uint256 phaseId, uint256 quantity, address currency,
   uint256 pricePerToken, (uint256 quantityLimit, uint256 price, address currency, bytes32[] proof) allowlistProof,
   bytes data)

CURRENCY_NATIVE is chain-specific (typically 0xEEEE...EE convention used by Sweep Haus).
If Sweep Haus ever changes the ABI, only this file needs updating.
"""


def _p32(val) -> str:
    """Zero-pad a value to 32 bytes (64 hex chars) for ABI encoding."""
    if isinstance(val, str):
        val = int(val, 16) if val.startswith("0x") else int(val)
    return f"{val:064x}"


def build_claim_calldata(
    recipient: str,
    sweep_fee_wei: int,
    currency_native_int: int,
    phase_id: int = 0,
    quantity: int = 1,
) -> str:
    """
    Build raw claim() calldata for a Sweep Haus collection.

    Args:
        recipient:           Wallet address (checksum or lowercase).
        sweep_fee_wei:       Platform fee in wei (e.g. 202000000000000).
        currency_native_int: Native token address as int (chain-specific).
        phase_id:            Drop phase ID (0 for most free mints).
        quantity:            Number of NFTs to claim (almost always 1).

    Returns:
        Hex-encoded calldata string starting with '0x'.
    """
    PROOF_MAX = 2**256 - 1
    recip = int(recipient.lower().replace("0x", ""), 16)

    return (
        "0x57bc3d78"
        + _p32(recip)                # arg0: recipient
        + _p32(phase_id)             # arg1: phaseId
        + _p32(quantity)             # arg2: quantity
        + _p32(currency_native_int)  # arg3: currency (native sentinel)
        + _p32(sweep_fee_wei)        # arg4: pricePerToken (platform fee)
        + _p32(224)                  # arg5: tuple offset (0xE0)
        + _p32(384)                  # arg6: bytes offset (0x180)
        # Allowlist proof struct (no allowlist = max values, empty proof array)
        + _p32(128)                  # bytes32[] offset within tuple (0x80)
        + _p32(PROOF_MAX)            # quantityLimitPerWallet = max_uint256
        + _p32(0)                    # pricePerToken in proof = 0
        + _p32(0)                    # currency in proof = address(0)
        + _p32(0)                    # bytes32[] length = 0
        + _p32(0)                    # arg6 bytes length = 0
    )
