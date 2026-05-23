"""
OmniRisk Health Calculator
Haraald / Head of IT

Parses on-chain obligation/account data from Kamino and MarginFi.
Calculates health factors and identifies liquidatable positions.

Health Factor < 1.0 = liquidatable
Health Factor < 1.05 = at risk (monitor closely)
"""

import struct
import logging
from dataclasses import dataclass
from typing import Optional
import base64

log = logging.getLogger("omnirisk.health")

# ─────────────────────────────────────────────────────────────────
# KAMINO PROGRAM IDs
# ─────────────────────────────────────────────────────────────────
KAMINO_PROGRAM_ID    = "KLend2g3cP87fffoy8q1mQqGKjrxjC8boSyAYavgmjD"
MARGINFI_PROGRAM_ID  = "MFv2hWf31Z9kbCa1snEPdcgp7X3yG3p3y3QA4ZLMSh"

# Liquidation bonuses by protocol
KAMINO_LIQ_BONUS_PCT  = 0.05   # 5% bonus
MARGINFI_LIQ_BONUS_PCT = 0.05  # 5% bonus

# Minimum position value to bother liquidating (USD)
MIN_LIQUIDATION_VALUE_USD = 50.0


@dataclass
class Position:
    """Represents a borrower position on a lending protocol."""
    protocol:        str         # "kamino" or "marginfi"
    account:         str         # on-chain account address
    owner:           str         # wallet that owns this position
    health_factor:   float       # < 1.0 = liquidatable
    collateral_usd:  float       # total collateral value in USD
    debt_usd:        float       # total debt value in USD
    liquidation_usd: float       # max we can liquidate
    bonus_usd:       float       # our profit from liquidating
    collateral_mint: str         # token we receive as liquidator
    debt_mint:       str         # token we repay as liquidator
    raw:             dict        # raw account data


def parse_kamino_obligation(account_data: dict, prices: dict) -> Optional[Position]:
    """
    Parse a Kamino obligation account and calculate health factor.
    
    Kamino stores obligations as borsh-serialized structs.
    We extract collateral and borrow amounts, apply prices,
    and calculate: health = (collateral * LTV) / debt
    """
    try:
        data_b64 = account_data.get("data", [None, None])[0]
        if not data_b64:
            return None

        raw_bytes = base64.b64decode(data_b64)
        pubkey    = account_data.get("pubkey", "")

        # Kamino obligation layout (simplified):
        # Offset 8:  owner pubkey (32 bytes)
        # Offset 40: lending market (32 bytes)
        # Offset 72: deposits array
        # Offset 200: borrows array
        # (Full parsing requires the IDL — we use API approach below)

        # For production, use Kamino's obligation API instead of
        # raw struct parsing — it's more reliable and handles updates
        return None  # Replaced by API-based parsing in scanner

    except Exception as e:
        log.debug(f"Kamino parse error: {e}")
        return None


def calculate_health_factor(
    collateral_usd: float,
    debt_usd: float,
    ltv: float = 0.80,
) -> float:
    """
    Health Factor = (Collateral USD * LTV) / Debt USD
    < 1.0 means the position is liquidatable.
    """
    if debt_usd <= 0:
        return 999.0  # No debt = perfectly healthy
    return (collateral_usd * ltv) / debt_usd


def estimate_liquidation_profit(
    position: dict,
    bonus_pct: float = 0.05,
    repay_fraction: float = 0.50,  # Max 50% of debt can be liquidated at once
) -> float:
    """
    Estimate our profit from liquidating a position.
    
    We repay up to 50% of the debt and receive collateral
    worth that amount + the liquidation bonus.
    
    Profit = repaid_amount * bonus_pct
    """
    debt_usd       = position.get("totalBorrowValueUsd", 0)
    max_repay      = debt_usd * repay_fraction
    bonus          = max_repay * bonus_pct
    return round(bonus, 4)
