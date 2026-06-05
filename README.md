# Sweep Haus NFT Auto-Minter Bot

Automatically discovers and mints free/cheap NFTs from [sweep.haus](https://sweep.haus/explore) across multiple chains and wallets.

---

## Quick Start

```bash
git clone <your-repo>
cd sweep-haus-bot
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
sweep-haus-bot/
├── main.py                  # Entry point — runs all wallets across all chains
├── minter.py                # Core minting engine (per-wallet logic)
├── sweep_api.py             # Sweep Haus API client + index caching
├── calldata.py              # ABI encoding / calldata builders
├── chain_config.py          # Chain loader from rpc.json
├── proxy.py                 # Proxy loader and rotation
├── config/
│   ├── rpc.json             # RPC endpoints per chain (mainnet + testnet)
│   ├── rpc.json.example     # Template
│   ├── config.json          # Per-chain mint settings
│   └── config.json.example  # Template
├── data/                    # Auto-created: sweep indexes, mint state
├── logs/                    # Auto-created: rotating log files
├── .env                     # Secrets (bearer tokens)
├── .env.example             # Template
├── pv.txt                   # Private keys (one per line)
├── pv.txt.example           # Template
├── proxy.txt                # Proxies (one per line, optional)
└── proxy.txt.example        # Template
```

---

## Configuration

### `.env` — Bearer Tokens

```env
# Single token
BEARER=your_token_here

# Multiple tokens (bot rotates round-robin)
BEARER_1=token_one
BEARER_2=token_two
BEARER_3=token_three
```

### `pv.txt` — Private Keys

One private key per line (with or without `0x` prefix):

```
0xabc123...
0xdef456...
```

> **Never commit this file.** It is in `.gitignore`.

### `proxy.txt` — Proxies (Optional)

One proxy per line. Format: `http://user:pass@host:port` or `http://host:port`

```
http://user:pass@1.2.3.4:8080
http://5.6.7.8:3128
```

Leave blank or omit file to run without proxies.

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
      "sweep_haus_chain_id": 10778
    },
    "base": {
      "chain_id": 8453,
      "name": "Base Mainnet",
      "type": "mainnet",
      "rpc": [
        "https://mainnet.base.org",
        "https://base.llamarpc.com"
      ],
      "explorer": "https://basescan.org",
      "sweep_haus_chain_id": 8453
    }
  }
}
```

### `config/config.json` — Mint Settings

```json
{
  "global": {
    "mode": "testnet",
    "concurrency": 3,
    "delay_between_wallets_sec": [5, 15],
    "cooldown_hours": 24,
    "target_mints_per_session": [1, 5]
  },
  "chains": {
    "x1_testnet": {
      "enabled": true,
      "max_price_native": 0.001,
      "max_price_usd_equiv": null,
      "gas_price_gwei": 1,
      "gas_limit": 280000,
      "gas_multiplier": 1.2
    },
    "base": {
      "enabled": false,
      "max_price_native": 0.0001,
      "max_price_usd_equiv": 0.001,
      "gas_price_gwei": null,
      "gas_limit": 300000,
      "gas_multiplier": 1.3
    }
  }
}
```

**`mode` options:**
- `"testnet"` — Only runs chains where `type = "testnet"` in rpc.json
- `"mainnet"` — Only runs chains where `type = "mainnet"`
- `"all"` — Runs everything that has `enabled: true`

**`max_price_native`** — Max price in native token (e.g. ETH, X1T). Set `null` to skip.
**`max_price_usd_equiv`** — Max price in USD equivalent. Requires price feed. Set `null` to skip.
**`gas_price_gwei`** — Set to `null` to use on-chain `eth_gasPrice` dynamically.
**`target_mints_per_session`** — `[min, max]` random range of NFTs to mint per wallet per session.

---

## How It Works

1. Bot loads all wallets from `pv.txt`
2. For each enabled chain (filtered by `mode`), it fetches active Sweep Haus collections from the API (cached 6h locally)
3. Each wallet checks its 24h cooldown, then mints 1–5 random NFTs it hasn't minted before
4. Mint state persists to `data/` — wallets never double-mint the same contract
5. Proxies rotate per wallet. Bearer tokens rotate round-robin per API call
6. All activity is logged to `logs/` with timestamps

---

## Risks & Limitations

- **Testnet only by default** — mainnet minting costs real gas. Enable explicitly in config.
- **USDC pricing** — `max_price_usd_equiv` filtering is USD-equivalent estimation only (uses native token price × amount). Actual USDC payment flows are not yet supported.
- **Sweep Haus API changes** — The API is undocumented. If they change endpoints or auth, update `sweep_api.py`.
- **Private keys in plaintext** — `pv.txt` is gitignored but still sensitive. Use a secrets manager for production.

---

## Legal / Ethical

This bot is for personal airdrop farming and educational use. Do not use it to DoS Sweep Haus APIs. The built-in jitter and rate limiting are there for a reason.
