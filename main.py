"""Compatibility entry point.

The implementation lives in the ``solana_monitor`` package; this shim only exists
so deployments that already run ``python main.py`` keep working. New setups
should install the project and use either ``python -m solana_monitor`` or the
``solana-monitor`` console script.
"""

from __future__ import annotations

from solana_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
