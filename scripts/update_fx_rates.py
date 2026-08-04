"""
Refresh data/fx_rates.json from a free public FX API.

Run this manually when you want current rates -- NOT automatically on every
query. See src/money.py for why the table is a pinned snapshot: a ranking that
silently changes because FX moved overnight is not reproducible, and no result
in the paper could be replayed.

    python -m scripts.update_fx_rates
    python -m scripts.update_fx_rates --dry-run

Uses open.er-api.com, which needs no API key. If it is unreachable the existing
file is left exactly as it was and the script exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timezone, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from src.config import settings  # noqa: E402
from src.logging_setup import configure_logging, get_logger  # noqa: E402
from src.money import FX_PATH  # noqa: E402

log = get_logger("scripts.update_fx_rates")

API = "https://open.er-api.com/v6/latest/USD"

# Only currencies the project actually needs. Keeping the table small makes the
# diff readable when you commit a refresh.
WANTED = ["USD", "INR", "EUR", "GBP", "AUD", "CAD", "JPY", "SGD", "AED"]


def fetch() -> dict[str, float]:
    resp = requests.get(
        API,
        timeout=settings.http_timeout_seconds,
        headers={"User-Agent": settings.user_agent},
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("result") != "success":
        raise RuntimeError(f"FX API returned result={payload.get('result')!r}")
    rates = payload["rates"]
    missing = [c for c in WANTED if c not in rates]
    if missing:
        raise RuntimeError(f"FX API response missing currencies: {missing}")
    return {c: round(float(rates[c]), 4) for c in WANTED}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would change, write nothing")
    args = ap.parse_args()

    configure_logging()

    old = json.loads(FX_PATH.read_text(encoding="utf-8")) if FX_PATH.is_file() else {}
    old_rates = old.get("rates", {})

    try:
        rates = fetch()
    except Exception as exc:
        log.error("FX refresh failed, existing table left untouched",
                  extra={"error": str(exc), "path": str(FX_PATH)})
        return 1

    payload = {
        "_comment": old.get(
            "_comment",
            "Static FX snapshot. Units of each currency per 1 USD.",
        ),
        "as_of": date.today().isoformat(),
        "base": "USD",
        "source": API,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rates": rates,
    }

    print(f"{'currency':<10}{'old':>12}{'new':>12}{'change':>10}")
    print("-" * 44)
    for cur in WANTED:
        o = old_rates.get(cur)
        n = rates[cur]
        if o:
            pct = (n - o) / o * 100
            print(f"{cur:<10}{o:>12.4f}{n:>12.4f}{pct:>9.2f}%")
        else:
            print(f"{cur:<10}{'-':>12}{n:>12.4f}{'new':>10}")

    if args.dry_run:
        print("\ndry run -- nothing written")
        return 0

    FX_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {FX_PATH}  (as_of {payload['as_of']})")
    print("Commit this file so rankings stay reproducible for whoever runs it next.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
