from __future__ import annotations

import argparse
import sys

from agent.config import Config
from agent.server.app import create_app
from agent.worker.runner import run_worker


def _run_server(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Install with: pip install '.[dev]'", file=sys.stderr)
        return 1

    app = create_app()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _run_worker(_: argparse.Namespace) -> int:
    cfg = Config.load()
    run_worker(cfg)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent", description="Coding Agents SDLC CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_p = subparsers.add_parser("server", help="Run webhook server")
    server_p.add_argument("--host", default="0.0.0.0")
    server_p.add_argument("--port", type=int, default=8000)
    server_p.add_argument("--log-level", default="info")
    server_p.set_defaults(func=_run_server)

    worker_p = subparsers.add_parser("worker", help="Run job worker")
    worker_p.set_defaults(func=_run_worker)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
