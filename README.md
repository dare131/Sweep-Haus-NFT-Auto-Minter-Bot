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

# 3. Run
python main.py
```

---

## File Structure

```
Sweep-Haus-NFT-Auto-Minter-Bot/
├── main.py                  # Entry point — runs all wallets across all chains
├── minter.py                # Core minting engine (per-wallet logic)
├── sweep_api.py             # Sweep Haus API client + index caching
├── calldata.py              # ABI encoding / calldata builders
├── chain_config.py          # Chain loader from rpc.json + config.json
├── proxy.py                 # Proxy loader and rotation
├── config/
│   ├── rpc.json             # RPC endpoints per chain (mainnet + testnet)
│   ├── rpc.json.example     # Template — copy this to rpc.json
│   ├── config.json          # Per-chain mint settings
│   └── config.json.example  # Template — copy this to config.json
├── data/                    # Auto-created: per-chain index files + mint state
│   ├── sweep_index_x1_testnet.json   # One index file per chain
│   ├── sweep_index_base_mainnet.json
│   └── sweep_minted.json             # Per-wallet mint history
├── logs/                    # Auto-created: rotating log files (10MB × 5)
├── .env                     # Secrets: bearer tokens (never commit)
├── .env.example             # Template
├── pv.txt                   # Private keys, one per line (never commit)
├── pv.txt.example           # Template
├── proxy.txt                # Proxies, one per line (optional)
└── proxy.txt.example        # Template
```

---

## Configuration

### `.env` — Bearer Tokens

Get your Sweep Haus bearer token from your browser's network tab while browsing sweep.haus.

```env
# Single token
BEARER=your_token_here

# Multiple tokens — bot rotates round-robin per API call
BEARER_1=token_one
BEARER_2=token_two
BEARER_3=token_three
```

### `pv.txt` — Private Keys

One private key per line. With or without `0x` prefix. Lines starting with `#` are ignored.

```
0xabc123...
0xdef456...
```

> **Never commit this file.** It is in `.gitignore` and must stay that way.

### `proxy.txt` — Proxies (Optional)

One proxy per line. Leave file empty or delete it to run without proxies (direct IP).

```
http://user:pass@1.2.3.4:8080
http://5.6.7.8:3128
socks5://user:pass@9.10.11.12:1080
```

Proxies are assigned per-wallet by index (wallet 0 → proxy 0, wallet 1 → proxy 1, wrapping around).

---

### `config/rpc.json` — Chain RPC Endpoints

Add any EVM chain Sweep Haus supports. The bot tries RPCs in the listed order and falls back automatically.

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

**Fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `chain_id` | ✅ | EVM chain ID |
| `type` | ✅ | `"testnet"` or `"mainnet"` — used by mode filter |
| `rpc` | ✅ | List of RPC URLs, tried in order on failure |
| `sweep_haus_chain_id` | ✅ | Sweep Haus internal chain filter ID (usually same as chain_id) |
| `native_symbol` | ✅ | Display label for logs (e.g. `"ETH"`, `"X1T"`) |
| `native_currency_address` | ✅ | Native token sentinel address (standard: `0xEeee...EE`) |

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
      "gas_buffer_gwei": 350000
    },
    "base_mainnet": {
      "enabled": false,
      "max_price_native": 0.00005,
      "gas_price_gwei": 0,
      "gas_limit": 300000,
      "gas_multiplier": 1.3,
      "priority_multiplier": 1.1,
      "gas_buffer_gwei": 500000
    }
  }
}
```

**Global settings:**

| Key | Description |
|-----|-------------|
| `mode` | `"testnet"` = only testnet chains, `"mainnet"` = only mainnet chains, `"all"` = everything enabled |
| `concurrency` | Wallets running in parallel per chain. Keep ≤5 to avoid RPC rate limits |
| `delay_between_wallets_sec` | `[min, max]` random delay (seconds) between wallet completions |
| `cooldown_hours` | Hours between sessions per wallet. Default 24 = once per day |
| `cooldown_on_fail` | `false` = cooldown only set on at least 1 success. `true` = always set cooldown |
| `target_mints_per_session` | `[min, max]` random number of NFTs to mint per wallet per session |
| `index_cache_hours` | How long to use cached collection list before re-fetching from API |
| `max_api_pages` | Max pages fetched from Sweep Haus API per refresh (16 items/page) |

**Per-chain settings:**

| Key | Description |
|-----|-------------|
| `enabled` | `true` to include this chain. Must also match `mode` filter |
| `max_price_native` | Max NFT price in native token. `null` = no price limit |
| `gas_price_gwei` | **`0` or `null` = use live on-chain gas price (recommended)**. Set a fixed number (e.g. `1`) only if you want to override |
| `gas_limit` | Max gas units per transaction. `280000` is safe for most Sweep Haus mints |
| `gas_multiplier` | `maxFeePerGas = base_gas × gas_multiplier`. Increase if txs underprice |
| `priority_multiplier` | `maxPriorityFeePerGas = base_gas × priority_multiplier` |
| `gas_buffer_gwei` | Gas units reserved in balance check (prevents minting when too low on gas funds) |

> **Gas tip:** Always leave `gas_price_gwei` at `0` unless you have a specific reason to fix it. The bot uses `eth_gasPrice` from the RPC, so it automatically adapts to network conditions.

---

## How It Works

1. Bot loads all wallets from `pv.txt` and all active chains from `config/rpc.json` filtered by `mode`
2. For each chain, active Sweep Haus collections are fetched from the API and cached locally to `data/sweep_index_{chain_key}.json` (refreshed every 6h by default)
3. Each wallet checks its 24h cooldown, then picks 1–5 random NFTs it hasn't minted before on that chain
4. Mint state persists to `data/sweep_minted.json` — wallets never double-mint the same contract
5. Proxies rotate per-wallet. Bearer tokens rotate round-robin per API call
6. All activity logged to `logs/sweep_bot.log` with rotation (10MB × 5 files)

---

## Data Files (auto-created)

| File | Description |
|------|-------------|
| `data/sweep_index_x1_testnet.json` | Collection index for X1 testnet (one file per chain) |
| `data/sweep_minted.json` | Per-wallet mint history, keyed by address + chain |

You can safely delete index files to force a fresh API fetch on next run. Do not delete `sweep_minted.json` unless you want wallets to re-mint already-minted collections.

---

## Risks & Limitations

- **Testnet only by default.** Set `"mode": "mainnet"` and `"enabled": true` on mainnet chains explicitly — mainnet minting costs real gas.
- **Sweep Haus API is undocumented.** If they change endpoints or auth, update `sweep_api.py`.
- **Private keys in plaintext.** `pv.txt` is gitignored but still sensitive. Do not run on shared/untrusted hosts.
- **Gas estimation fallback.** If `estimate_gas()` fails (e.g. contract would revert), bot falls back to config `gas_limit`. A tx can still be submitted and revert on-chain.
- **No USDC payment support.** Collections requiring ERC-20 payment are not handled. Native-token-only mints work.

---

## License & Disclaimer

**Educational License & Terms of Use**

This software is provided "as is", without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and non-infringement. In no event shall the authors or copyright holders be liable for any claim, damages, or other liability, whether in an action of contract, tort or otherwise, arising from, out of, or in connection with the software or the use or other dealings in the software.

MIT License — See [LICENSE](https://github.com/dare131/Sweep-Haus-NFT-Auto-Minter-Bot/blob/main/LICENSE) for full terms.
