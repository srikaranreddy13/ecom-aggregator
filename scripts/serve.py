"""
Start the API server.

    python -m scripts.serve
    python -m scripts.serve --reload --port 9000

Equivalent to `uvicorn src.api.app:app`, but it checks the dependency is
installed first and prints the URLs you actually need, rather than failing with
a bare ImportError.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default=settings.api_host)
    ap.add_argument("--port", type=int, default=settings.api_port)
    ap.add_argument("--reload", action="store_true",
                    help="restart on code changes (development only)")
    args = ap.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn is not installed. Install the API extras:\n")
        print("    python -m pip install fastapi uvicorn pydantic\n")
        print("or everything at once:\n")
        print("    python -m pip install -r requirements.txt")
        return 1

    print(f"  API      http://{args.host}:{args.port}")
    print(f"  Docs     http://{args.host}:{args.port}/docs")
    print(f"  Health   http://{args.host}:{args.port}/health")
    print(f"  Try      http://{args.host}:{args.port}/search?q=iphone&price=0.9")
    print()

    uvicorn.run(
        "src.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_config=None,       # our own logging config owns formatting
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
