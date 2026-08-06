#!/usr/bin/env python
"""Launch the Polybot dashboard.

    python run.py                 # http://127.0.0.1:8848
    python run.py --port 9000
    python run.py --no-autostart  # start the engine manually from the UI

On loopback the server has no authentication, which is why a routable bind is
refused by default: it would expose engine control — and a wallet key — to the
whole network. Set POLYBOT_PASSWORD to put every route behind HTTP Basic, and
the bind is then permitted. --i-understand-the-risk still forces it through
without a password, for a host that is private by other means.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Polybot 3.0 — Polymarket short-window dashboard")
    parser.add_argument("--host", default=os.getenv("POLYBOT_HOST", "127.0.0.1"))
    # PORT is what container platforms inject, and it is the one they route to;
    # ignoring it would leave the app healthy on a port nothing reaches.
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT") or os.getenv("POLYBOT_PORT") or 8848),
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on source changes")
    parser.add_argument("--no-autostart", action="store_true", help="do not start the engine on boot")
    parser.add_argument(
        "--i-understand-the-risk",
        action="store_true",
        help="permit binding to a non-loopback interface",
    )
    args = parser.parse_args()

    loopback = {"127.0.0.1", "localhost", "::1"}
    authenticated = bool(os.getenv("POLYBOT_PASSWORD", "").strip())
    if args.host not in loopback and not authenticated and not args.i_understand_the_risk:
        print(
            f"Refusing to bind to {args.host}: this server can start the engine, move every\n"
            f"risk limit and hold a wallet key, and no password is set. Set POLYBOT_PASSWORD\n"
            f"to enable HTTP Basic auth, use 127.0.0.1, or pass --i-understand-the-risk.",
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

    gate = "password required" if authenticated else "no authentication"
    print(f"\n  Polybot 3.0  ->  http://{args.host}:{args.port}  ({gate})\n")
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
