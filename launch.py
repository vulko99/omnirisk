"""
OmniRisk Full Stack Launcher v2.0
Haraald / Head of IT

Runs all three engines simultaneously:
1. ARB Scanner     — finds price gaps across DEXes
2. ARB Executor    — fires arb trades via Jito
3. LIQ Scanner     — monitors Kamino + MarginFi health
4. LIQ Executor    — fires liquidations via Jito

One command. Everything starts. Everything stops together.
Press Ctrl+C to stop all.
"""

import asyncio
import subprocess
import sys
import os
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("omnirisk.launcher")

REQUIRED_FILES = [
    "arb_scanner.py",
    "executor.py",
    "liquidation_scanner.py",
    "liquidation_executor.py",
    "wallet_manager.py",
    "jito_client.py",
    "tx_builder.py",
    "guardian.py",
    "health_calculator.py",
]


def check_env() -> bool:
    issues = []

    for f in REQUIRED_FILES:
        if not os.path.exists(f):
            issues.append(f"Missing file: {f}")

    if not os.path.exists("wallet.key") and not os.environ.get("OMNIRISK_PRIVATE_KEY"):
        issues.append("No wallet — create wallet.key or set OMNIRISK_PRIVATE_KEY")

    helius = os.environ.get("HELIUS_RPC_URL", "")
    if not helius or "YOUR_HELIUS_KEY_HERE" in helius:
        issues.append("Set HELIUS_RPC_URL — get free key at helius.dev")

    if issues:
        print("\n" + "=" * 55)
        print("  LAUNCH BLOCKED — Fix these issues:")
        print("=" * 55)
        for i in issues:
            print(f"  - {i}")
        print("=" * 55 + "\n")
        return False
    return True


async def stream_output(proc, prefix: str, color: str = ""):
    """Stream subprocess output with prefix label."""
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        print(f"[{prefix}] {line.decode().rstrip()}")


async def run():
    if not check_env():
        return

    log.info("=" * 55)
    log.info("  OmniRisk Full Stack v2.0")
    log.info(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("  Engines: ARB Scanner | ARB Executor")
    log.info("           LIQ Scanner | LIQ Executor")
    log.info("=" * 55)

    processes = []

    # Start all 4 engines
    engines = [
        ("arb_scanner.py",        "ARB-SCAN"),
        ("executor.py",           "ARB-EXEC"),
        ("liquidation_scanner.py","LIQ-SCAN"),
        ("liquidation_executor.py","LIQ-EXEC"),
    ]

    for script, label in engines:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        processes.append((proc, label))
        log.info(f"  Started {label} (PID: {proc.pid})")

    log.info("=" * 55)
    log.info("  All engines running. Ctrl+C to stop.")
    log.info("=" * 55)

    # Stream all output
    try:
        await asyncio.gather(*[
            stream_output(proc, label)
            for proc, label in processes
        ])
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Shutting down all engines...")
        for proc, label in processes:
            try:
                proc.terminate()
                await proc.wait()
                log.info(f"  {label} stopped")
            except Exception:
                pass
        log.info("All engines stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nStopped.")
