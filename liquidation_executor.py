"""
OmniRisk Liquidation Executor v1.0
Haraald / Head of IT

Monitors the liquidation_opportunities table and executes
liquidations on Kamino and MarginFi via Jito bundles.

Profit comes from the liquidation bonus — we repay debt
and receive collateral worth more than we paid.
"""

import asyncio
import aiohttp
import sqlite3
import time
import logging
import sys
import os
import json
import base64
from datetime import datetime, timezone
from typing import Optional

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

HELIUS_RPC_URL = os.environ.get(
    "HELIUS_RPC_URL",
    "https://mainnet.helius-rpc.com/?api-key=YOUR_HELIUS_KEY_HERE"
)

DB_PATH = "arb_opportunities.db"

# Minimum bonus we'll bother executing for
MIN_BONUS_USD       = 2.0    # $2 minimum — covers gas
POLL_INTERVAL_MS    = 500
MAX_POSITION_AGE_S  = 30     # Don't liquidate if opp is stale

# Kamino liquidation program
KAMINO_PROGRAM_ID   = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
MARGINFI_PROGRAM_ID = "MFv2hWf31Z9kbCa1snEPdcgp7X3yG3p3y3QA4ZLMSh"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("liquidation_executor.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("omnirisk.liq_executor")


# ─────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────
def get_pending_liquidations(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("""
        SELECT id, ts, protocol, account, owner,
               health_factor, collateral_usd, debt_usd,
               liquidation_usd, bonus_usd,
               collateral_mint, debt_mint
        FROM liquidation_opportunities
        WHERE status = 'pending'
          AND health_factor < 1.0
          AND bonus_usd >= ?
        ORDER BY bonus_usd DESC
        LIMIT 10
    """, (MIN_BONUS_USD,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def mark_liq_executed(
    conn: sqlite3.Connection,
    liq_id: int,
    tx_hash: str,
    actual_profit: float,
    status: str,
):
    conn.execute("""
        UPDATE liquidation_opportunities
        SET status=?, tx_hash=?, actual_profit=?
        WHERE id=?
    """, (status, tx_hash, actual_profit, liq_id))
    conn.commit()


def mark_liq_failed(conn: sqlite3.Connection, liq_id: int, reason: str):
    conn.execute("""
        UPDATE liquidation_opportunities
        SET status='failed'
        WHERE id=?
    """, (liq_id,))
    conn.commit()


# ─────────────────────────────────────────────────────────────────
# KAMINO LIQUIDATION TX BUILDER
# ─────────────────────────────────────────────────────────────────
async def build_kamino_liquidation_tx(
    session: aiohttp.ClientSession,
    liq: dict,
    wallet_pubkey: str,
    rpc_url: str,
) -> Optional[bytes]:
    """
    Build a Kamino liquidation transaction.

    Steps:
    1. Get obligation account data
    2. Identify worst underwater position
    3. Build liquidateObligationAndRedeemReserveCollateral instruction
    4. Add Jito tip instruction
    5. Return serialized TX
    """
    try:
        # Get obligation account on-chain
        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "getAccountInfo",
            "params":  [
                liq["account"],
                {"encoding": "base64", "commitment": "confirmed"},
            ],
        }
        async with session.post(
            rpc_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            data = await r.json()
            account_info = data.get("result", {}).get("value")
            if not account_info:
                log.warning(f"Obligation not found: {liq['account'][:12]}...")
                return None

        # Use Kamino's liquidation API to get the TX
        # Kamino provides a convenience endpoint that builds the IX for us
        liq_payload = {
            "obligationAddress":     liq["account"],
            "liquidatorAuthority":   wallet_pubkey,
            "repayReserve":          liq.get("debt_mint", ""),
            "withdrawReserve":       liq.get("collateral_mint", ""),
            "liquidityAmount":       str(int(liq["liquidation_usd"] * 1_000_000)),
        }

        async with session.post(
            "https://api.kamino.finance/v2/liquidation/transaction",
            json=liq_payload,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Content-Type": "application/json"},
        ) as r:
            if r.status == 200:
                resp = await r.json()
                tx_b64 = resp.get("transaction")
                if tx_b64:
                    return base64.b64decode(tx_b64)
            else:
                text = await r.text()
                log.warning(f"Kamino liq TX error {r.status}: {text[:100]}")

    except Exception as e:
        log.error(f"Kamino TX build error: {e}")

    return None


async def build_marginfi_liquidation_tx(
    session: aiohttp.ClientSession,
    liq: dict,
    wallet_pubkey: str,
    rpc_url: str,
) -> Optional[bytes]:
    """
    Build a MarginFi liquidation transaction.
    Uses MarginFi's liquidation endpoint to get the serialized TX.
    """
    try:
        liq_payload = {
            "liquidateeMarginfiAccount": liq["account"],
            "liquidatorAuthority":       wallet_pubkey,
            "assetBankPk":               liq.get("collateral_mint", ""),
            "liabilityBankPk":           liq.get("debt_mint", ""),
            "liquidatorAssetTokenAccount": wallet_pubkey,
            "liquidatorLiabilityTokenAccount": wallet_pubkey,
            "maxLiquidatableAssetAmount": str(int(liq["liquidation_usd"] * 1_000_000)),
        }

        async with session.post(
            f"{MARGINFI_API_BASE}/liquidate/transaction",
            json=liq_payload,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Content-Type": "application/json"},
        ) as r:
            if r.status == 200:
                resp = await r.json()
                tx_b64 = resp.get("transaction")
                if tx_b64:
                    return base64.b64decode(tx_b64)
            else:
                text = await r.text()
                log.warning(f"MarginFi liq TX error {r.status}: {text[:100]}")

    except Exception as e:
        log.error(f"MarginFi TX build error: {e}")

    return None


# ─────────────────────────────────────────────────────────────────
# MAIN EXECUTOR
# ─────────────────────────────────────────────────────────────────
class LiquidationExecutor:
    def __init__(self):
        from wallet_manager import Wallet
        from jito_client import JitoClient
        from guardian import Guardian

        self.wallet   = Wallet()
        self.jito     = JitoClient()
        self.guardian = Guardian()
        self.conn     = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.total_liquidations  = 0
        self.total_profit        = 0.0

    async def start(self):
        if not self.wallet.load():
            log.error("Wallet not loaded — cannot execute liquidations")
            return

        log.info("=" * 65)
        log.info("   OmniRisk Liquidation Executor v1.0")
        log.info(f"   Wallet: {self.wallet.public_key[:8]}...{self.wallet.public_key[-6:]}")
        log.info(f"   Min bonus: ${MIN_BONUS_USD}")
        log.info("=" * 65)

        connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
        async with aiohttp.ClientSession(connector=connector) as session:
            await self.run_loop(session)

    async def run_loop(self, session: aiohttp.ClientSession):
        while True:
            t0 = time.time()

            if self.guardian.is_kill_switch_active():
                await asyncio.sleep(5)
                continue

            pending = get_pending_liquidations(self.conn)

            for liq in pending:
                # Check if opportunity is still fresh
                try:
                    ts  = datetime.fromisoformat(liq["ts"].replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age > MAX_POSITION_AGE_S:
                        log.debug(f"Stale liq skipped: {liq['account'][:12]}...")
                        continue
                except Exception:
                    continue

                await self.execute_liquidation(session, liq)
                await asyncio.sleep(0.2)

            elapsed    = time.time() - t0
            sleep_time = max(0, (POLL_INTERVAL_MS / 1000) - elapsed)
            await asyncio.sleep(sleep_time)

    async def execute_liquidation(
        self,
        session: aiohttp.ClientSession,
        liq: dict,
    ):
        protocol = liq["protocol"]
        liq_id   = liq["id"]
        bonus    = liq["bonus_usd"]

        log.info(
            f"[LIQUIDATE] {protocol.upper()} | "
            f"{liq['account'][:12]}... | "
            f"HF: {liq['health_factor']:.3f} | "
            f"Debt: ${liq['debt_usd']:.0f} | "
            f"Bonus: ${bonus:.2f}"
        )

        # Guardian check
        approved, reason = self.guardian.approve({
            "profit_usd": bonus,
            "profit_bps": bonus / max(liq["debt_usd"], 1) * 10000,
        })
        if not approved:
            log.info(f"[GUARDIAN] Blocked: {reason}")
            return

        # Build TX based on protocol
        tx_bytes = None
        if protocol == "kamino":
            tx_bytes = await build_kamino_liquidation_tx(
                session, liq, self.wallet.public_key, HELIUS_RPC_URL
            )
        elif protocol == "marginfi":
            tx_bytes = await build_marginfi_liquidation_tx(
                session, liq, self.wallet.public_key, HELIUS_RPC_URL
            )

        if not tx_bytes:
            log.warning(f"TX build failed for {liq['account'][:12]}...")
            mark_liq_failed(self.conn, liq_id, "TX_BUILD_FAILED")
            self.guardian.record_failure()
            return

        # Sign
        try:
            signed_tx = self.wallet.sign_transaction(tx_bytes)
        except Exception as e:
            log.error(f"Signing error: {e}")
            mark_liq_failed(self.conn, liq_id, str(e))
            return

        # Submit via Jito
        bundle_id = await self.jito.send_bundle(session, [signed_tx])

        if not bundle_id:
            log.warning("Bundle submission failed")
            mark_liq_failed(self.conn, liq_id, "SUBMIT_FAILED")
            self.guardian.record_failure()
            return

        # Wait for confirmation
        await asyncio.sleep(3)
        status = await self.jito.get_bundle_status(session, bundle_id)

        if status and status.get("confirmation_status") in ("confirmed", "finalized"):
            self.total_liquidations += 1
            self.total_profit       += bonus
            self.guardian.record_success()

            log.info(
                f"[SUCCESS] Liquidation confirmed | "
                f"+${bonus:.2f} | "
                f"Total P&L: ${self.total_profit:.2f}"
            )
            mark_liq_executed(
                self.conn, liq_id,
                bundle_id, bonus, "success"
            )
        else:
            log.warning(f"Liquidation unconfirmed: {bundle_id[:16]}...")
            mark_liq_failed(self.conn, liq_id, "UNCONFIRMED")
            self.guardian.record_failure()


if __name__ == "__main__":
    executor = LiquidationExecutor()
    asyncio.run(executor.start())
