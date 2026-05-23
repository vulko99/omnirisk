"""
OmniRisk Guardian v1.0 — Autonomous Risk Management
Haraald / Head of IT

Monitors the scanner DB and applies safety limits before
the executor module is allowed to fire live transactions.
"""

import sqlite3
import time
import logging
from datetime import datetime, timedelta

DB_PATH = "arb_opportunities.db"

log = logging.getLogger("omnirisk.guardian")

# ─────────────────────────────────────────────────────────────────
# RISK LIMITS
# ─────────────────────────────────────────────────────────────────
MAX_GAS_SOL_PER_HOUR   = 0.10    # Max SOL spent on gas per hour
MAX_FAILED_TX_RATE     = 0.40    # Kill if >40% of TXs fail in last 30 min
MAX_CONSECUTIVE_FAILS  = 5       # Hard stop after 5 consecutive fails
MIN_PROFIT_USD         = 0.05    # Never fire TX for less than $0.05 net
MAX_DAILY_SPEND_SOL    = 0.50    # Hard daily gas cap

KILL_SWITCH_FILE       = ".kill_switch"

# ─────────────────────────────────────────────────────────────────
class Guardian:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.consecutive_fails = 0
        self.killed = False

    def is_kill_switch_active(self) -> bool:
        """Check for manual kill switch file."""
        import os
        return os.path.exists(KILL_SWITCH_FILE)

    def approve(self, opp: dict) -> tuple[bool, str]:
        """
        Returns (approved, reason).
        Call this before every live TX attempt.
        """
        if self.killed:
            return False, "GUARDIAN: Hard kill active"

        if self.is_kill_switch_active():
            self.killed = True
            log.critical("🔴 KILL SWITCH FILE DETECTED — all execution halted")
            return False, "Kill switch file active"

        # Check minimum profit
        if opp.get("profit_usd", 0) < MIN_PROFIT_USD:
            return False, f"Profit ${opp['profit_usd']:.4f} below minimum ${MIN_PROFIT_USD}"

        # Check hourly gas spend
        gas_spent = self._gas_spent_last_hour()
        if gas_spent >= MAX_GAS_SOL_PER_HOUR:
            return False, f"Hourly gas cap reached ({gas_spent:.4f} SOL)"

        # Check daily gas spend
        daily_gas = self._gas_spent_today()
        if daily_gas >= MAX_DAILY_SPEND_SOL:
            return False, f"Daily gas cap reached ({daily_gas:.4f} SOL)"

        # Check consecutive fail rate
        if self.consecutive_fails >= MAX_CONSECUTIVE_FAILS:
            self.killed = True
            log.critical(f"🔴 {MAX_CONSECUTIVE_FAILS} consecutive fails — killing executor")
            return False, "Consecutive fail limit hit"

        # Check rolling fail rate
        fail_rate = self._fail_rate_last_30min()
        if fail_rate > MAX_FAILED_TX_RATE:
            return False, f"TX fail rate {fail_rate:.0%} exceeds {MAX_FAILED_TX_RATE:.0%}"

        return True, "OK"

    def record_success(self):
        self.consecutive_fails = 0

    def record_failure(self):
        self.consecutive_fails += 1
        log.warning(f"⚠️  Consecutive fails: {self.consecutive_fails}/{MAX_CONSECUTIVE_FAILS}")

    def _gas_spent_last_hour(self) -> float:
        """Placeholder — will query tx logs once executor is live."""
        return 0.0

    def _gas_spent_today(self) -> float:
        return 0.0

    def _fail_rate_last_30min(self) -> float:
        cutoff = (datetime.utcnow() - timedelta(minutes=30)).isoformat()
        cur = self.conn.execute(
            "SELECT executed FROM opportunities WHERE ts > ? AND executed IS NOT NULL",
            (cutoff,)
        )
        rows = cur.fetchall()
        if not rows:
            return 0.0
        failed = sum(1 for r in rows if r[0] == -1)
        return failed / len(rows)

    def status_report(self) -> dict:
        cur = self.conn.execute(
            "SELECT COUNT(*), SUM(profit_usd) FROM opportunities WHERE executed=1"
        )
        row = cur.fetchone()
        return {
            "live_executions":   row[0] or 0,
            "total_live_profit": round(row[1] or 0, 4),
            "consecutive_fails": self.consecutive_fails,
            "killed":            self.killed,
            "kill_file_active":  self.is_kill_switch_active(),
        }


def activate_kill_switch():
    """Call this from any process to immediately halt execution."""
    with open(KILL_SWITCH_FILE, "w") as f:
        f.write(datetime.utcnow().isoformat())
    log.critical("🔴 KILL SWITCH ACTIVATED")


def deactivate_kill_switch():
    import os
    if os.path.exists(KILL_SWITCH_FILE):
        os.remove(KILL_SWITCH_FILE)
        log.info("✅ Kill switch deactivated")
