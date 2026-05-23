"""
OmniRisk Liquidation Scanner v1.0
Haraald / Head of IT

Monitors Kamino Finance and MarginFi for undercollateralized positions.
When health factor drops below 1.0, queues position for liquidation.

Uses Kamino and MarginFi public APIs + Helius RPC for on-chain data.
"""

import asyncio
import aiohttp
import sqlite3
import time
import logging
import sys
import os
from datetime import datetime, timezone
from typing import Optional

from health_calculator import (
    KAMINO_PROGRAM_ID,
    MARGINFI_PROGRAM_ID,
    MIN_LIQUIDATION_VALUE_USD,
    KAMINO_LIQ_BONUS_PCT,
    MARGINFI_LIQ_BONUS_PCT,
    calculate_health_factor,
    estimate_liquidation_profit,
)

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# ─────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────
HELIUS_RPC_URL = os.environ.get(
    "HELIUS_RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY_HERE"
)

# How often to scan all positions
SCAN_INTERVAL_SECONDS = 15

# Health factor threshold — below this we flag for liquidation
LIQUIDATION_THRESHOLD = 1.0

# We also monitor "at risk" positions approaching liquidation
AT_RISK_THRESHOLD     = 1.05

DB_PATH = "arb_opportunities.db"

# Kamino API endpoints
KAMINO_API_BASE        = "https://api.kamino.finance"
KAMINO_OBLIGATIONS_URL = f"{KAMINO_API_BASE}/v2/obligations"
KAMINO_MARKETS_URL     = f"{KAMINO_API_BASE}/v2/lending-markets"

# MarginFi API
MARGINFI_API_BASE      = "https://marginfi-api.rpcpool.com"

# ─────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("liquidation_scanner.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("omnirisk.liq_scanner")


# ─────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────
def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS liquidation_opportunities (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT    NOT NULL,
            protocol         TEXT    NOT NULL,
            account          TEXT    NOT NULL,
            owner            TEXT,
            health_factor    REAL    NOT NULL,
            collateral_usd   REAL    NOT NULL,
            debt_usd         REAL    NOT NULL,
            liquidation_usd  REAL    NOT NULL,
            bonus_usd        REAL    NOT NULL,
            collateral_mint  TEXT,
            debt_mint        TEXT,
            status           TEXT    DEFAULT 'pending',
            tx_hash          TEXT,
            actual_profit    REAL,
            created_at       TEXT    DEFAULT (datetime('now'))
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS liq_scan_stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              TEXT    NOT NULL,
            total_scans     INTEGER NOT NULL,
            positions_found INTEGER NOT NULL,
            liq_found       INTEGER NOT NULL,
            at_risk_found   INTEGER NOT NULL,
            total_bonus_usd REAL    NOT NULL
        )
    """)

    conn.commit()
    return conn


def save_liq_opportunity(conn: sqlite3.Connection, pos: dict):
    # Check if already saved (same account, still pending)
    existing = conn.execute(
        "SELECT id FROM liquidation_opportunities WHERE account=? AND status='pending'",
        (pos["account"],)
    ).fetchone()

    if existing:
        # Update health factor
        conn.execute(
            "UPDATE liquidation_opportunities SET health_factor=?, ts=? WHERE id=?",
            (pos["health_factor"], pos["ts"], existing[0])
        )
    else:
        conn.execute("""
            INSERT INTO liquidation_opportunities
                (ts, protocol, account, owner, health_factor,
                 collateral_usd, debt_usd, liquidation_usd,
                 bonus_usd, collateral_mint, debt_mint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            pos["ts"], pos["protocol"], pos["account"],
            pos.get("owner"), pos["health_factor"],
            pos["collateral_usd"], pos["debt_usd"],
            pos["liquidation_usd"], pos["bonus_usd"],
            pos.get("collateral_mint"), pos.get("debt_mint"),
        ))
    conn.commit()


# ─────────────────────────────────────────────────────────────────
# KAMINO SCANNER
# ─────────────────────────────────────────────────────────────────
async def get_kamino_markets(session: aiohttp.ClientSession) -> list[str]:
    """Get list of active Kamino lending markets."""
    try:
        async with session.get(
            KAMINO_MARKETS_URL,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status == 200:
                data = await r.json()
                markets = []
                for m in data:
                    if m.get("lendingMarketAddress"):
                        markets.append(m["lendingMarketAddress"])
                return markets
    except Exception as e:
        log.debug(f"Kamino markets error: {e}")
    return []


async def scan_kamino(
    session: aiohttp.ClientSession,
    conn: sqlite3.Connection,
) -> tuple[int, int, float]:
    """
    Scan Kamino obligations for liquidatable positions.
    Returns (positions_checked, liq_found, total_bonus_usd)
    """
    positions_checked = 0
    liq_found         = 0
    total_bonus       = 0.0

    try:
        # Get unhealthy obligations via Kamino API
        params = {
            "status": "unhealthy",
            "limit":  "100",
        }
        async with session.get(
            KAMINO_OBLIGATIONS_URL,
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                log.debug(f"Kamino obligations status: {r.status}")
                return 0, 0, 0.0

            data = await r.json()
            obligations = data if isinstance(data, list) else data.get("obligations", [])

            for obl in obligations:
                positions_checked += 1

                collateral_usd  = float(obl.get("totalDepositValueUsd", 0))
                debt_usd        = float(obl.get("totalBorrowValueUsd", 0))
                health_factor   = float(obl.get("loanToValue", 999))

                # Kamino returns LTV not health factor — convert
                # Health = 1 / LTV (when LTV > max_ltv, position is liquidatable)
                max_ltv         = float(obl.get("maxLoanToValue", 0.80))
                if max_ltv > 0:
                    health_factor = max_ltv / health_factor if health_factor > 0 else 999

                if health_factor >= AT_RISK_THRESHOLD:
                    continue

                if debt_usd < MIN_LIQUIDATION_VALUE_USD:
                    continue

                # Calculate our profit
                bonus_usd       = estimate_liquidation_profit(obl, KAMINO_LIQ_BONUS_PCT)
                liquidation_usd = debt_usd * 0.5  # Max 50% close factor

                is_liquidatable = health_factor < LIQUIDATION_THRESHOLD

                # Get token info
                deposits = obl.get("deposits", [])
                borrows  = obl.get("borrows", [])
                col_mint = deposits[0].get("mintAddress", "") if deposits else ""
                dbt_mint = borrows[0].get("mintAddress", "") if borrows else ""

                pos = {
                    "ts":              datetime.now(timezone.utc).isoformat(),
                    "protocol":        "kamino",
                    "account":         obl.get("obligationAddress", ""),
                    "owner":           obl.get("owner", ""),
                    "health_factor":   round(health_factor, 4),
                    "collateral_usd":  round(collateral_usd, 2),
                    "debt_usd":        round(debt_usd, 2),
                    "liquidation_usd": round(liquidation_usd, 2),
                    "bonus_usd":       round(bonus_usd, 4),
                    "collateral_mint": col_mint,
                    "debt_mint":       dbt_mint,
                }

                save_liq_opportunity(conn, pos)
                total_bonus += bonus_usd

                if is_liquidatable:
                    liq_found += 1
                    log.info(
                        f"[KAMINO LIQ] {pos['account'][:12]}... | "
                        f"HF: {health_factor:.3f} | "
                        f"Debt: ${debt_usd:.0f} | "
                        f"Bonus: ${bonus_usd:.2f}"
                    )
                else:
                    log.info(
                        f"[KAMINO RISK] {pos['account'][:12]}... | "
                        f"HF: {health_factor:.3f} | "
                        f"Debt: ${debt_usd:.0f}"
                    )

    except Exception as e:
        log.warning(f"Kamino scan error: {e}")

    return positions_checked, liq_found, total_bonus


# ─────────────────────────────────────────────────────────────────
# MARGINFI SCANNER
# ─────────────────────────────────────────────────────────────────
async def scan_marginfi(
    session: aiohttp.ClientSession,
    conn: sqlite3.Connection,
) -> tuple[int, int, float]:
    """
    Scan MarginFi accounts for liquidatable positions.
    Uses MarginFi's public API to get account health data.
    """
    positions_checked = 0
    liq_found         = 0
    total_bonus       = 0.0

    try:
        async with session.get(
            f"{MARGINFI_API_BASE}/accounts/liquidatable",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            if r.status != 200:
                log.debug(f"MarginFi API status: {r.status}")
                return 0, 0, 0.0

            data = await r.json()
            accounts = data if isinstance(data, list) else data.get("accounts", [])

            for acc in accounts:
                positions_checked += 1

                health_factor  = float(acc.get("healthFactor", 999))
                collateral_usd = float(acc.get("totalAssetValueUsd", 0))
                debt_usd       = float(acc.get("totalLiabilityValueUsd", 0))

                if health_factor >= AT_RISK_THRESHOLD:
                    continue

                if debt_usd < MIN_LIQUIDATION_VALUE_USD:
                    continue

                bonus_usd       = debt_usd * 0.5 * MARGINFI_LIQ_BONUS_PCT
                liquidation_usd = debt_usd * 0.5
                is_liquidatable = health_factor < LIQUIDATION_THRESHOLD

                pos = {
                    "ts":              datetime.now(timezone.utc).isoformat(),
                    "protocol":        "marginfi",
                    "account":         acc.get("address", ""),
                    "owner":           acc.get("authority", ""),
                    "health_factor":   round(health_factor, 4),
                    "collateral_usd":  round(collateral_usd, 2),
                    "debt_usd":        round(debt_usd, 2),
                    "liquidation_usd": round(liquidation_usd, 2),
                    "bonus_usd":       round(bonus_usd, 4),
                    "collateral_mint": acc.get("collateralMint", ""),
                    "debt_mint":       acc.get("debtMint", ""),
                }

                save_liq_opportunity(conn, pos)
                total_bonus += bonus_usd

                if is_liquidatable:
                    liq_found += 1
                    log.info(
                        f"[MARGINFI LIQ] {pos['account'][:12]}... | "
                        f"HF: {health_factor:.3f} | "
                        f"Debt: ${debt_usd:.0f} | "
                        f"Bonus: ${bonus_usd:.2f}"
                    )

    except Exception as e:
        log.warning(f"MarginFi scan error: {e}")

    return positions_checked, liq_found, total_bonus


# ─────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────
async def run():
    conn = init_db()

    log.info("=" * 65)
    log.info("   OmniRisk Liquidation Scanner v1.0")
    log.info(f"   Scan interval: {SCAN_INTERVAL_SECONDS}s")
    log.info(f"   Min position: ${MIN_LIQUIDATION_VALUE_USD}")
    log.info(f"   Liq threshold: HF < {LIQUIDATION_THRESHOLD}")
    log.info(f"   At-risk threshold: HF < {AT_RISK_THRESHOLD}")
    log.info("=" * 65)

    total_scans     = 0
    total_liq_found = 0
    total_bonus_usd = 0.0

    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            t0 = time.time()
            total_scans += 1

            log.info(f"[SCAN #{total_scans}] Scanning Kamino + MarginFi...")

            # Scan both protocols in parallel
            kamino_task   = scan_kamino(session, conn)
            marginfi_task = scan_marginfi(session, conn)

            (k_pos, k_liq, k_bonus), (m_pos, m_liq, m_bonus) = await asyncio.gather(
                kamino_task, marginfi_task
            )

            scan_liq   = k_liq + m_liq
            scan_bonus = k_bonus + m_bonus
            total_liq_found += scan_liq
            total_bonus_usd += scan_bonus

            log.info(
                f"[STATS] Scan #{total_scans} | "
                f"Positions: {k_pos + m_pos} | "
                f"Liquidatable: {scan_liq} | "
                f"Potential bonus: ${scan_bonus:.2f} | "
                f"Total found: {total_liq_found}"
            )

            # Save stats
            conn.execute("""
                INSERT INTO liq_scan_stats
                    (ts, total_scans, positions_found, liq_found,
                     at_risk_found, total_bonus_usd)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                total_scans, k_pos + m_pos,
                scan_liq, 0, total_bonus_usd,
            ))
            conn.commit()

            elapsed    = time.time() - t0
            sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
            await asyncio.sleep(sleep_time)


if __name__ == "__main__":
    asyncio.run(run())
