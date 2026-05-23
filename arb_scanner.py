"""
OmniRisk Arbitrage Scanner v1.0
Haraald / Head of IT

Round-trip arbitrage detection via Jupiter Quote API.
Compares direct route vs reverse route for each pair.
Logs all profitable opportunities to SQLite.
"""

import asyncio
import aiohttp
import time
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
JUPITER_QUOTE_URL  = "https://lite-api.jup.ag/swap/v1/quote"
SCAN_INTERVAL_MS   = 500
MIN_PROFIT_BPS     = -9999             # lowered to 0.05% to catch more
FLASH_LOAN_FEE_BPS = 9             # 0.09% Kamino/Solend
MAX_SLIPPAGE_BPS   = 50
USDC_AMOUNT        = 500_000_000   # 500 USDC (6 decimals)
SOL_AMOUNT         = 1_000_000_000 # 1 SOL (9 decimals)
DB_PATH            = "arb_opportunities.db"

# ─────────────────────────────────────────────────────────────────
# TOKEN MINTS
# ─────────────────────────────────────────────────────────────────
USDC  = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL   = "So11111111111111111111111111111111111111112"
USDT  = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
BONK  = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
WIF   = "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"
JUP   = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"
RAY   = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"

# Pairs: (token_a, token_b, label, amount_of_a)
PAIRS = [
    (USDC, SOL,  "USDC/SOL",  USDC_AMOUNT),
    (USDC, USDT, "USDC/USDT", USDC_AMOUNT),
    (USDC, BONK, "USDC/BONK", USDC_AMOUNT),
    (USDC, WIF,  "USDC/WIF",  USDC_AMOUNT),
    (USDC, JUP,  "USDC/JUP",  USDC_AMOUNT),
    (USDC, RAY,  "USDC/RAY",  USDC_AMOUNT),
    (SOL,  USDC, "SOL/USDC",  SOL_AMOUNT),
    (SOL,  BONK, "SOL/BONK",  SOL_AMOUNT),
    (SOL,  WIF,  "SOL/WIF",   SOL_AMOUNT),
]

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("omnirisk.arb")


# ─────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opportunities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            pair        TEXT    NOT NULL,
            dex_buy     TEXT    NOT NULL,
            dex_sell    TEXT    NOT NULL,
            amount_in   INTEGER NOT NULL,
            amount_mid  INTEGER NOT NULL,
            amount_out  INTEGER NOT NULL,
            gross_bps   REAL    NOT NULL,
            flash_fee   INTEGER NOT NULL,
            profit_bps  REAL    NOT NULL,
            profit_usd  REAL    NOT NULL,
            route_buy   TEXT,
            route_sell  TEXT,
            executed    INTEGER DEFAULT 0,
            tx_hash     TEXT,
            created_at  TEXT    DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scan_stats (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            total_scans INTEGER NOT NULL,
            opps_found  INTEGER NOT NULL,
            sim_pnl     REAL    NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_opportunity(conn: sqlite3.Connection, opp: dict):
    conn.execute("""
        INSERT INTO opportunities
            (ts, pair, dex_buy, dex_sell, amount_in, amount_mid, amount_out,
             gross_bps, flash_fee, profit_bps, profit_usd, route_buy, route_sell)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        opp["ts"], opp["pair"], opp["dex_buy"], opp["dex_sell"],
        opp["amount_in"], opp["amount_mid"], opp["amount_out"],
        opp["gross_bps"], opp["flash_fee"],
        opp["profit_bps"], opp["profit_usd"],
        opp.get("route_buy"), opp.get("route_sell"),
    ))
    conn.commit()


# ─────────────────────────────────────────────────────────────────
# QUOTE FETCHER — no DEX filter, let Jupiter find best route
# ─────────────────────────────────────────────────────────────────
async def get_quote(
    session: aiohttp.ClientSession,
    input_mint: str,
    output_mint: str,
    amount: int,
    direct_only: bool = False,
) -> Optional[dict]:
    params = {
        "inputMint":        input_mint,
        "outputMint":       output_mint,
        "amount":           str(amount),
        "slippageBps":      str(MAX_SLIPPAGE_BPS),
        "maxAccounts":      "64",
    }
    if direct_only:
        params["onlyDirectRoutes"] = "true"

    try:
        async with session.get(
            JUPITER_QUOTE_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=4),
        ) as r:
            if r.status == 200:
                data = await r.json()
                if "error" not in data and data.get("outAmount"):
                    return data
    except asyncio.TimeoutError:
        log.info("Quote timeout")
    except Exception as e:
        log.info(f"Quote error: {e}")
    return None


def extract_route_label(quote: dict) -> str:
    try:
        plans = quote.get("routePlan", [])
        if plans:
            labels = [p.get("swapInfo", {}).get("label", "?") for p in plans]
            return " > ".join(labels)
    except Exception:
        pass
    return "Unknown"


def usdc_decimals(pair_name: str) -> bool:
    return pair_name.startswith("USDC")


# ─────────────────────────────────────────────────────────────────
# ARB DETECTOR — round-trip: A -> B -> A
# ─────────────────────────────────────────────────────────────────
async def check_arb(
    session: aiohttp.ClientSession,
    token_a: str,
    token_b: str,
    pair_name: str,
    amount: int,
) -> list[dict]:
    opportunities = []

    # Step 1: A -> B (best Jupiter route)
    quote_ab = await get_quote(session, token_a, token_b, amount)
    if not quote_ab:
        log.info(f"[FAIL AB] {pair_name} - no quote A->B")
        return []

    mid_amount = int(quote_ab.get("outAmount", 0))
    if mid_amount == 0:
        return []

    route_buy = extract_route_label(quote_ab)

    # Step 2: B -> A (best Jupiter route, different path)
    quote_ba = await get_quote(session, token_b, token_a, mid_amount)
    if not quote_ba:
        log.info(f"[FAIL BA] {pair_name} - no quote B->A")
        return []

    final_amount = int(quote_ba.get("outAmount", 0))
    route_sell   = extract_route_label(quote_ba)

    # Step 3: Profit calculation
    gross_profit  = final_amount - amount
    flash_fee_amt = int(amount * FLASH_LOAN_FEE_BPS / 10_000)
    net_profit    = gross_profit - flash_fee_amt

    # Log every round-trip for visibility (debug)
    gross_bps_raw = round((gross_profit / amount) * 10_000, 2)
    log.info(f"[CHECK] {pair_name} | gross: {gross_bps_raw} bps | net after flash fee: {round((net_profit/amount)*10_000,2)} bps")

    if net_profit <= 0:
        return []

    gross_bps  = round((gross_profit / amount) * 10_000, 2)
    profit_bps = round((net_profit / amount) * 10_000, 2)

    if profit_bps < MIN_PROFIT_BPS:
        return []

    # Normalize to USD
    if usdc_decimals(pair_name):
        profit_usd = round(net_profit / 1_000_000, 4)
    else:
        profit_usd = round(net_profit / 1_000_000_000 * 150, 4)

    opp = {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "pair":       pair_name,
        "dex_buy":    route_buy,
        "dex_sell":   route_sell,
        "amount_in":  amount,
        "amount_mid": mid_amount,
        "amount_out": final_amount,
        "gross_bps":  gross_bps,
        "flash_fee":  flash_fee_amt,
        "profit_bps": profit_bps,
        "profit_usd": profit_usd,
        "route_buy":  route_buy,
        "route_sell": route_sell,
    }
    opportunities.append(opp)

    log.info(
        f"[ARB FOUND] {pair_name:<12} | "
        f"BUY  {route_buy[:20]:<20} | "
        f"SELL {route_sell[:20]:<20} | "
        f"+{profit_bps:.2f} bps | ${profit_usd:.4f}"
    )

    return opportunities


# ─────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────
async def run():
    conn = init_db()

    log.info("=" * 65)
    log.info("   OmniRisk Arbitrage Scanner v1.0  - Paper Trading Mode")
    log.info(f"   Pairs: {len(PAIRS)} | Min profit: {MIN_PROFIT_BPS} bps | Flash fee: {FLASH_LOAN_FEE_BPS} bps")
    log.info(f"   Scan interval: {SCAN_INTERVAL_MS}ms | Slippage max: {MAX_SLIPPAGE_BPS} bps")
    log.info("=" * 65)

    total_scans = 0
    total_found = 0
    total_pnl   = 0.0
    t_start     = time.time()

    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            t0 = time.time()

            tasks   = [check_arb(session, ta, tb, nm, am) for ta, tb, nm, am in PAIRS]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, list):
                    for opp in result:
                        save_opportunity(conn, opp)
                        total_found += 1
                        total_pnl   += opp["profit_usd"]

            total_scans += 1
            runtime      = time.time() - t_start

            if total_scans % 30 == 0:
                conn.execute(
                    "INSERT INTO scan_stats (ts, total_scans, opps_found, sim_pnl) VALUES (?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), total_scans, total_found, total_pnl)
                )
                conn.commit()
                log.info(
                    f"[STATS] Scans: {total_scans:,} | "
                    f"Opps: {total_found} | "
                    f"Sim P&L: ${total_pnl:.4f} | "
                    f"Runtime: {runtime:.0f}s"
                )

            elapsed    = time.time() - t0
            sleep_time = max(0, (SCAN_INTERVAL_MS / 1000) - elapsed)
            await asyncio.sleep(sleep_time)


if __name__ == "__main__":
    asyncio.run(run())