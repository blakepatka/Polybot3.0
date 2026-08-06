#!/usr/bin/env python
"""Launch the Polybot dashboard.

    python run.py                 # http://127.0.0.1:8848
    python run.py --port 9000
    python run.py --no-autostart  # start the engine manually from the UI

The server has no authentication. It binds to loopback and refuses any other
interface unless you pass --i-understand-the-risk, because a routable bind
would expose engine control — and a wallet key — to the whole network.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Polybot 3.0 — Polymarket short-window dashboard")
    parser.add_argument("--host", default=os.getenv("POLYBOT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("POLYBOT_PORT", "8848")))
    parser.add_argument("--reload", action="store_true", help="auto-reload on source changes")
    parser.add_argument("--no-autostart", action="store_true", help="do not start the engine on boot")
    parser.add_argument(
        "--i-understand-the-risk",
        action="store_true",
        help="permit binding to a non-loopback interface",
    )
    args = parser.parse_args()

    loopback = {"127.0.0.1", "localhost", "::1"}
    if args.host not in loopback and not args.i_understand_the_risk:
        print(
            f"Refusing to bind to {args.host}: this server has no authentication and can hold\n"
            f"wallet credentials. Use 127.0.0.1, or pass --i-understand-the-risk to override.",
            file=sys.stderr,
        )
        return 2

    if args.no_autostart:
        os.environ["POLYBOT_AUTOSTART"] = "0"

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 1

    # Windows consoles still default to cp1252, which cannot encode most
    # non-ASCII output. Reconfigure where possible and keep this banner ASCII.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    print(f"\n  Polybot 3.0  ->  http://{args.host}:{args.port}\n")
    uvicorn.run(
        "polybot.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
