# OmniRisk Arbitrage Scanner v1.0
**Haraald / Head of IT**

Fully autonomous Solana DEX arbitrage scanner.
Monitors price inefficiencies across Raydium, Orca, Meteora DLMM,
Raydium CLMM, Lifinity V2 via Jupiter Quote API.

---

## Quick Start

```bash
# Install deps
pip install -r requirements.txt

# Phase 1+2: Paper trading (NO real transactions)
python arb_scanner.py

# View logged opportunities
sqlite3 arb_opportunities.db "SELECT * FROM opportunities ORDER BY ts DESC LIMIT 20;"
```

## Architecture

```
arb_scanner.py   — Core scanner engine (always running)
guardian.py      — Risk management & kill switch
requirements.txt — Python dependencies
arb_opportunities.db — SQLite log (auto-created)
scanner.log      — Full execution log
.kill_switch     — Create this file to halt execution instantly
```

## Phase Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 1     | ✅ Ready | Scanner + opportunity logger |
| 2     | ✅ Ready | Profit simulation with flash fee calculation |
| 3     | 🔒 Locked | Live TX execution via Jito bundles |
| 4     | 🔒 Locked | OmniRisk dashboard integration |

## Kill Switch

To immediately halt all execution from any terminal:

```bash
touch .kill_switch        # halt
rm .kill_switch           # resume
```

Or from Python:
```python
from guardian import activate_kill_switch, deactivate_kill_switch
activate_kill_switch()
```

## Config (arb_scanner.py)

| Parameter | Default | Description |
|-----------|---------|-------------|
| MIN_PROFIT_BPS | 10 | Min net profit (0.10%) |
| FLASH_LOAN_FEE_BPS | 9 | Kamino/Solend flash fee |
| MAX_SLIPPAGE_BPS | 30 | Abort threshold |
| SCAN_INTERVAL_MS | 500 | Poll frequency |
| USDC_AMOUNT | 500 USDC | Trade size for USDC pairs |

## Safety

- Phase 1+2 are **read-only**. No wallets, no transactions.
- Guardian module enforces hourly gas caps, daily limits,
  consecutive fail limits, and a hard kill switch file.
- Never run Phase 3 without at least 2 weeks of paper data.
