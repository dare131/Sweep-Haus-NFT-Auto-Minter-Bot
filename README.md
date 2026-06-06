# Sweep Haus NFT Auto-Minter Bot

Automatically discovers and mints free/cheap NFTs from [sweep.haus](https://sweep.haus/explore) across multiple chains and wallets.

---

> **⚠️ Important — Read Before Using**
>
> **Educational & Research Purpose Only.** This repository is created strictly for educational, academic, and research purposes. It is designed as a proof-of-concept to study Web3 automated interactions, programmatic transaction flows, and decentralized network behaviors on testnets. It is not intended for commercial use or any activity that violates third-party terms of service. The authors are not responsible for any misuse, account bans, fund loss, or restrictions. **Use of this codebase is entirely at your own risk.**

---

## Quick Start

```bash
git clone https://github.com/dare131/Sweep-Haus-NFT-Auto-Minter-Bot
cd Sweep-Haus-NFT-Auto-Minter-Bot
pip install -r requirements.txt

# 1. Copy and fill in your config
cp .env.example .env
cp config/rpc.json.example config/rpc.json
cp config/config.json.example config/config.json

# 2. Add wallets and proxies
cp pv.txt.example pv.txt
cp proxy.txt.example proxy.txt

# 3. Test your setup first (no transactions sent)
python main.py --dry-run

# 4. Run live
python main.py
```

---

## Usage

```bash
# Single run — live transactions
python main.py

# Dry run — full pipeline, no transactions sent (safe for config testing)
python main.py --dry-run

# Loop every 24h (reads cooldown_hours from config)
python main.py --loop

# Loop every 12h
python main.py --loop --interval 12

# Dry-run loop — test config changes safely
python main.py --dry-run --loop --interval 1
```

**Environment variables (alternative to CLI flags):**
```env
LOOP=true
LOOP_INTERVAL_HOURS=24
```

---

## File Structure

```
Sweep-Haus-NFT-Auto-Minter-Bot/
├── main.py                  # Entry point — CLI args, wallet loader, thread pool, loop
├── minter.py                # Core minting engine (per-wallet, per-chain logic)
├── sweep_api.py             # Sweep Haus API client + per-chain index caching
├── calldata.py              # ABI encoding / calldata builders
├── chain_config.py          # Chain loader from rpc.json + config.json
├── proxy.py                 # Proxy loader and rotation
├── config/
│   ├── rpc.json             # RPC endpoints per chain (edit this)
│   ├── rpc.json.example     # Template — copy to rpc.json
│   ├── config.json          # Per-chain mint settings (edit this)
│   └── config.json.example  # Template — copy to config.json
├── data/                    # Auto-created on first run
│   ├── sweep_index_x1_testnet.json    # Collection index per chain
│   ├── sweep_index_base_mainnet.json
│   └── minted_0xabcd1234.json         # Mint history per wallet
├── logs/                    # Auto-created: rotating logs (10MB × 5 files)
│   └── sweep_bot.log
├── .env                     # Bearer tokens — never commit
├── .env.example
├── pv.txt                   # Private keys — never commit
├── pv.txt.example
├── proxy.txt                # Proxies (optional)
└── proxy.txt.example
```

---

## Configuration

### `.env` — Bearer Tokens

Get your Sweep Haus bearer token from your browser's DevTools → Network tab while browsing sweep.haus (look for requests to `api.sweep.haus`).

```env
# Single token
BEARER=your_token_here

# Multiple tokens — bot rotates round-robin per API call
BEARER_1=token_one
BEARER_2=token_two
BEARER_3=token_three
```

If a token expires mid-run, the bot logs a clear 401 error and skips that chain instead of silently using stale data.

---

### `pv.txt` — Private Keys

One private key per line. With or without `0x` prefix. Lines starting with `#` are ignored.

```
0xabc123...
0xdef456...
```

> **Never commit this file.** It is in `.gitignore`. Do not run on shared or untrusted hosts.

---

### `proxy.txt` — Proxies (Optional)

One proxy per line. Leave file empty or delete it to run without proxies.

```
http://user:pass@1.2.3.4:8080
http://5.6.7.8:3128
socks5://user:pass@9.10.11.12:1080
```

Proxies are assigned per-wallet by index — wallet 0 → proxy 0, wallet 1 → proxy 1, wrapping around. Same wallet always uses the same proxy across runs.

---

### `config/rpc.json` — Chain RPC Endpoints

```json
{
  "chains": {
    "x1_testnet": {
      "chain_id": 10778,
      "name": "X1 EcoChain Testnet",
      "type": "testnet",
      "rpc": [
        "https://x1-testnet.xen.network/",
        "https://x1testnet.xenfyi.com/"
      ],
      "explorer": "https://explorer.x1-testnet.xen.network",
      "sweep_haus_chain_id": 10778,
      "native_symbol": "X1T",
      "native_currency_address": "0xEeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
    },
    "base_mainnet": {
      "chain_id": 8453,
      "name": "Base Mainnet",
      "type": "mainnet",
      "rpc": [
        "https://mainnet.base.org",
        "https://base.llamarpc.com"
      ],
      "explorer": "https://basescan.org",
      "sweep_haus_chain_id": 8453,
      "native_symbol": "ETH",
      "native_currency_address": "0xEeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
    }
  }
}
```

| Field | Required | Description |
|---|---|---|
| `chain_id` | ✅ | EVM chain ID — bot validates this against the RPC response |
| `type` | ✅ | `"testnet"` or `"mainnet"` — used by the `mode` filter |
| `rpc` | ✅ | List of RPC URLs, tried in order on failure |
| `sweep_haus_chain_id` | ✅ | Chain ID used in the Sweep Haus API filter (usually same as chain_id) |
| `native_symbol` | ✅ | Display label in logs (e.g. `"ETH"`, `"X1T"`) |
| `native_currency_address` | ✅ | Native token sentinel (`0xEeee...EE` standard) |

---

### `config/config.json` — Mint Settings

```json
{
  "global": {
    "mode": "testnet",
    "concurrency": 3,
    "delay_between_wallets_sec": [5, 15],
    "cooldown_hours": 24,
    "cooldown_on_fail": false,
    "target_mints_per_session": [1, 5],
    "index_cache_hours": 6,
    "max_api_pages": 3
  },
  "chains": {
    "x1_testnet": {
      "enabled": true,
      "max_price_native": 0.001,
      "gas_price_gwei": 0,
      "gas_limit": 280000,
      "gas_multiplier": 1.2,
      "priority_multiplier": 1.1,
      "gas_buffer_units": 350000
    },
    "base_mainnet": {
      "enabled": false,
      "max_price_native": 0.00005,
      "gas_price_gwei": 0,
      "gas_limit": 300000,
      "gas_multiplier": 1.3,
      "priority_multiplier": 1.1,
      "gas_buffer_units": 500000
    }
  }
}
```

**Global settings:**

| Key | Description |
|---|---|
| `mode` | `"testnet"` = only testnet chains, `"mainnet"` = only mainnet, `"all"` = everything enabled |
| `concurrency` | Wallets running in parallel per chain. Keep ≤ 5 to avoid RPC rate limits |
| `delay_between_wallets_sec` | `[min, max]` random delay in seconds between wallet completions |
| `cooldown_hours` | Hours between sessions per wallet. Default `24` = once per day |
| `cooldown_on_fail` | `false` = cooldown only set if ≥1 mint succeeded. `true` = always set cooldown |
| `target_mints_per_session` | `[min, max]` random NFT count per wallet per session |
| `index_cache_hours` | How long to use cached collection list before re-fetching from API |
| `max_api_pages` | Max pages fetched from Sweep Haus API per refresh (16 collections/page) |

**Per-chain settings:**

| Key | Description |
|---|---|
| `enabled` | `true` to include this chain. Must also match `mode` filter |
| `max_price_native` | Max NFT price in native token. `null` = no limit |
| `gas_price_gwei` | **`0` or `null` = use live on-chain gas price (recommended)**. Set a number to fix it |
| `gas_limit` | Max gas units per transaction. `280000` safe for most Sweep Haus mints |
| `gas_multiplier` | `maxFeePerGas = base_gas × gas_multiplier` |
| `priority_multiplier` | `maxPriorityFeePerGas = base_gas × priority_multiplier` |
| `gas_buffer_units` | Gas units reserved for balance check buffer. `buffer_wei = gas_buffer_units × base_gas_price` |

> **Gas tip:** Always leave `gas_price_gwei` at `0`. The bot fetches live gas from the RPC, adapting automatically. Only override if you have a specific reason.

---

## How It Works

1. Bot loads wallets from `pv.txt` and active chains from `config/rpc.json` (filtered by `mode`)
2. For each chain, active Sweep Haus collections are fetched from the API and cached to `data/sweep_index_{chain_key}.json` — one file per chain, refreshed every 6h
3. Each wallet checks its 24h cooldown, then picks 1–5 random collections it hasn't minted on that chain
4. Per-wallet mint state is stored in `data/minted_{address}.json` — wallets never double-mint the same contract
5. On each successful mint, the ERC-721 Transfer event in the receipt is verified to confirm the NFT landed in the correct wallet
6. Proxies rotate per-wallet. Bearer tokens rotate round-robin per API call
7. All activity is logged to `logs/sweep_bot.log` with rotation

---

## Data Files

| File | Description |
|---|---|
| `data/sweep_index_{chain_key}.json` | Collection index per chain. Delete to force fresh API fetch |
| `data/minted_{address[:10]}.json` | Per-wallet mint history. Delete only if you want wallets to re-mint |

---

## Risks & Limitations

- **Testnet default.** Enable mainnet chains explicitly — real gas is at stake.
- **Sweep Haus API is undocumented.** If they change endpoints or auth, update `sweep_api.py`.
- **Private keys in plaintext.** `pv.txt` is gitignored. Do not run on shared/untrusted hosts.
- **Gas estimation fallback.** If `estimate_gas()` fails, the bot falls back to config `gas_limit`. A tx may still be submitted and revert.
- **No ERC-20 payment support.** Collections requiring USDC/ERC-20 payment are not handled. Native-token-only mints only.

---

## Support the Project

If this bot saves you time, tips are appreciated — any EVM network:

```
0x4f6Fb0A6c8A4C667bdF73C0257BE162B144c1624
```

ETH / Base / Arbitrum / Optimism / Polygon or any EVM chain.

---

## License & Disclaimer

**Educational License & Terms of Use**

This software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement. In no event shall the authors or copyright holders be liable for any claim, damages, or other liability, whether in an action of contract, tort or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software.

MIT License — See [LICENSE](https://github.com/dare131/Sweep-Haus-NFT-Auto-Minter-Bot/blob/main/LICENSE) for full terms.
