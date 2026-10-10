"""Run the existing site locally without collectors, trading executors or training.

Windows uses the application's real kernel-backed file locks. Collectors,
execution workers, research and training remain disabled in this local server.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8774)
    parser.add_argument("--research-history", action="store_true", help="Enable personal loopback-only historical stock charts from the verified native snapshot")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    root = Path(__file__).resolve().parents[1]
    local_dependencies = root / "output" / "pytest-deps"
    if local_dependencies.is_dir():
        sys.path.insert(0, str(local_dependencies))
    sys.path.insert(0, str(root))
    if os.name == "nt":
        if not hasattr(os, "getuid"):
            os.getuid = lambda: 1000
    from mom_trading.app import create_app
    app = create_app(start_background=False, start_paper_forward=False)
    app.config["RESEARCH_WORKBENCH_ROOT"] = root
    if args.research_history:
        from mom_trading.local_research_history import install
        install(app, root)
        from mom_trading.local_data_connections import install as install_connections
        install_connections(app, root)
        from mom_trading.local_factor_inputs import install as install_factor_inputs
        install_factor_inputs(app, root)
        from mom_trading.local_daily_factors import install as install_daily_factors
        install_daily_factors(app, root)
        from mom_trading.local_matched_research import install as install_matched
        install_matched(app, root)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
