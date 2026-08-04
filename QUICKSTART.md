# Quickstart

## The one-click way

Double-click **`run.bat`**.

It checks Python, installs dependencies, runs the tests, generates 180 days of
price history, prints the pipeline walkthrough and the evaluation tables, builds
the dashboard, and opens it. If anything fails it tells you which step and why.

---

## The step-by-step way

Run these in order, from the folder that contains `src`, `scripts` and `tests`.

### Step 0 — get into the right folder

```
cd C:\Users\srika\Downloads\project-v2\ecom-aggregator
dir
```

You should see `src`, `scripts`, `tests`, `web`, `run.bat`, `README.md`.

If instead you see a single nested folder, the zip nested itself — go one level
deeper. **Every command below assumes you are in this folder.** Almost every
error in this project comes from being in the wrong directory.

### Step 1 — install dependencies

```
python -m pip install -r requirements.txt
```

If that fails on `sentence-transformers`, that is expected on Python 3.14 (no
PyTorch wheels yet) and **nothing is blocked** — install just the essentials:

```
python -m pip install numpy scikit-learn requests pytest fastapi uvicorn pydantic httpx
```

### Step 2 — use the fast backend

```
set EMBEDDER_BACKEND=tfidf
```

(PowerShell instead of cmd: `$env:EMBEDDER_BACKEND="tfidf"`)

This skips a slow import. It lasts only for the current terminal window, so
re-run it if you open a new one. To make it permanent, copy `.env.example` to
`.env` — it is already set there.

### Step 3 — run the tests

```
python -m pytest tests -q
```

Expect **145 passed**. If you see 11, you are running an older copy.

### Step 4 — generate the price history

```
python -m scripts.backfill_prices --days 180
```

This is the step that did not exist before. It produces ~4,800 observations
across 180 days with realistic price movement. It is deterministic, so re-running
gives the identical series, and safe to re-run.

Check the last two lines of the output:

```
  flat (unforecastable): 0
```

If that number is not zero, some series have no price variance and cannot be
forecast. Audit them with:

```
python -m scripts.backfill_prices --purge-flat --dry-run
```

### Step 5 — see the pipeline in the terminal

```
python -m scripts.demo
```

Four stages: ingestion, entity resolution, MCDM ranking, price history.

### Step 6 — the evaluation tables

```
python -m scripts.evaluate_matching
python -m scripts.analyse_normalisation
```

The first prints the entity-resolution ablation. The second prints the refined
normalisation result — read its conclusion, it corrects an over-general claim
from the earlier version of this project.

### Step 7 — start the API

```
python -m scripts.serve
```

Then open **http://127.0.0.1:8000/docs** for the interactive OpenAPI UI.

Try these directly in a browser:

```
http://127.0.0.1:8000/health
http://127.0.0.1:8000/search?q=iphone&price=0.9&delivery_days=0.05&seller_rating=0.05
http://127.0.0.1:8000/search?q=iphone&price=0.05&delivery_days=0.05&seller_rating=0.9
http://127.0.0.1:8000/tracked-items?min_observations=100
```

The last two use different weights on the same query and return **different
winners**. That is the MCDM module working.

### Step 8 — build the static dashboard (optional)

```
python -m scripts.build_dashboard
```

Writes `dashboard.html`. Double-click it — no server needed. This is a snapshot;
the API in Step 7 is the live version.

### Step 9 — start collecting real price data

```
python -m scripts.collect_prices
```

Then schedule it daily (Windows Task Scheduler / cron). Until you configure eBay
credentials this collects from the synthetic market and says so.

---

## Reading `/health`

`/health` is the fastest way to know whether the system is in a good state.

```json
{
  "status": "degraded",
  "price_history": { "observations": 4842, "distinct_days": 180,
                     "synthetic_observations": 4842, "real_observations": 0,
                     "flat_series": 0 },
  "warnings": ["all price observations are synthetic ...",
               "serving results from the synthetic market"]
}
```

`status: degraded` is **expected** until you have live credentials — it is not a
malfunction. The two numbers to watch:

- **`flat_series`** must be `0`. Anything else means unforecastable data.
- **`real_observations`** is `0` until eBay credentials are configured. Any
  forecast accuracy figure computed while this is `0` measures the simulator,
  not the market.

---

## Troubleshooting

**`No module named 'scripts'`**
Wrong folder. Run `dir` — if you do not see a `scripts` folder, `cd` into the one
that has it.

**`No module named 'numpy'` / `'sklearn'`**
Dependencies not installed. Go back to Step 1.

**`No module named 'fastapi'`**
Only needed for the API. `python -m pip install fastapi uvicorn pydantic httpx`.
Everything except Steps 7 works without it.

**The terminal hangs for ~30 seconds with no output**
`sentence-transformers` is loading PyTorch and possibly downloading a model.
Either wait it out once, or `set EMBEDDER_BACKEND=tfidf` first.

**`python -m scripts.serve` says the port is in use**
`python -m scripts.serve --port 9000`

**Opening `web/template.html` shows "not yet built"**
Correct — that is the source template with no data in it. Run Step 8 and open
`dashboard.html` instead.

**`python` is not recognised**
Python is not on your PATH. Reinstall from python.org and tick "Add python.exe
to PATH".

**`/health` says `flat_series` is not 0**
Leftover rows from the old static-file collector.
`python -m scripts.backfill_prices --purge-flat`

**Tests show 11 passed instead of 145**
You have an older copy of the project.

---

## Optional: real embeddings

The project ships with a TF-IDF fallback so it runs anywhere with no downloads.

```
python -m pip install sentence-transformers
set EMBEDDER_BACKEND=transformer
python -m scripts.evaluate_matching
```

PyTorch has no wheels for Python 3.14 yet. If the install fails, that is why —
install Python 3.12 alongside it and use that interpreter. The TF-IDF path keeps
working in the meantime, so nothing is blocked.

The similarity threshold adjusts automatically per backend
(`EntityResolver.DEFAULT_THRESHOLD`), so you do not need to re-tune anything.

---

## Optional: live eBay data

1. Register at https://developer.ebay.com and create a **Production** keyset.
2. Copy `.env.example` to `.env` and fill in:
   ```
   EBAY_CLIENT_ID=your-id
   EBAY_CLIENT_SECRET=your-secret
   ```
3. Restart the API. `/health` will show the `ebay` adapter, and the synthetic
   market warning will disappear.

The eBay path is tested against a saved response fixture
(`tests/test_ebay_adapter.py`), so the normalisation logic is verified before
you spend a single API call on it.

---

## Optional: Docker

```
docker compose run --rm backfill      # populate price history
docker compose up --build             # API on http://localhost:8000
```

The database lives in a named volume, so `docker compose down` does not destroy
your price history. Use `down -v` only when you mean to.

---

## What each file does

| Path | Purpose |
|---|---|
| `run.bat` | One-click: install, test, backfill, demo, evaluate, build, open |
| `.env.example` | Every tunable, documented. Copy to `.env` |
| `src/config.py` | All settings, read from the environment in one place |
| `src/logging_setup.py` | Structured logging (`LOG_FORMAT=json` for aggregators) |
| `src/money.py` | Currency normalisation before ranking |
| `src/pipeline.py` | fetch → normalise → cache → resolve → rank |
| `src/adapters/base.py` | The one interface every source implements |
| `src/adapters/http.py` | Retry, backoff, jitter, per-host rate limiting |
| `src/adapters/errors.py` | Typed failures, so retry decisions are correct |
| `src/adapters/synthetic_adapter.py` | Deterministic market simulator |
| `src/adapters/ebay_adapter.py` | Live eBay Browse API client |
| `src/matching/` | Attribute extraction, embeddings, entity resolution |
| `src/ranking/mcdm.py` | TOPSIS, both normalisation modes, weighted-sum baseline |
| `src/storage/db.py` | SQLite cache, price history, schema migrations |
| `src/api/app.py` | HTTP endpoints |
| `src/api/schemas.py` | Request/response contract, generates `/docs` |
| `scripts/serve.py` | Start the API |
| `scripts/backfill_prices.py` | Generate / audit / purge price history |
| `scripts/collect_prices.py` | Daily snapshot — schedule this |
| `scripts/analyse_normalisation.py` | The refined Result 2 |
| `scripts/update_fx_rates.py` | Refresh the FX snapshot |
| `tests/` | 145 tests |
