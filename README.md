# AI-Driven E-Commerce Aggregator and Hyper-Personalized Recommendation Engine

Project Work — CVR College of Engineering, Dept. of CSE (AY 2026-27)

Cross-marketplace product aggregation with semantic entity resolution and
multi-criteria (TOPSIS) ranking, served over an HTTP API.

**Status:** aggregation, entity resolution and ranking are complete and tested.
Recommendation and price forecasting are not built yet — but the data
infrastructure they need now exists and works.

> **Explaining this project to someone?** Read **[PROJECT_NOTES.md](PROJECT_NOTES.md)**.
> It walks through what was broken, why each bug mattered, and the reasoning
> behind each fix — in narrative order, with the questions you will be asked and
> a demo running order. This README is the reference; those notes are the story.

---

## Run it

Windows: double-click **`run.bat`** — installs, tests, generates price history,
runs the demo and evaluations, builds the dashboard, opens it.

Anything else:

```bash
pip install -r requirements.txt
export EMBEDDER_BACKEND=tfidf              # Windows: set EMBEDDER_BACKEND=tfidf

python -m pytest tests -q                  # 145 tests
python -m scripts.backfill_prices --days 180   # generate price history
python -m scripts.demo                     # terminal walkthrough
python -m scripts.serve                    # start the API -> localhost:8000/docs
```

Step-by-step commands and troubleshooting: **QUICKSTART.md**

---

## What each part does

```
   eBay API    sim_discount  sim_warehouse  sim_standard  sim_express
   (live)      \______________ synthetic market ______________/
       |                              |
       +---------------+--------------+          <- SourceAdapter implementations
                       |
              CURRENCY NORMALISATION              <- src/money.py
                       |
               RESPONSE CACHE (SQLite)            <- src/storage/db.py
                       |
                 [ Listing ]  canonical schema
                       |
       Stage 1: ATTRIBUTE BLOCKING                <- storage, colour, brand
                       |
       Stage 2: SEMANTIC CLUSTERING               <- embeddings + agglomerative
                       |
                [ ProductCluster ]
                       |
       Stage 3: MCDM RANKING (TOPSIS)             <- user weight vector
                       |
            ranked offers + explanation           <- HTTP API / dashboard
```

Every source implements one interface, so the ML code never learns whether a
listing came from an API, a simulator, or a cache.

| Module | Files | Status |
|---|---|---|
| Source adapters | `src/adapters/base.py`, `mock_adapter.py`, `ebay_adapter.py` | Working |
| Synthetic market | `src/adapters/synthetic_adapter.py` | Working |
| HTTP resilience (retry, rate limit) | `src/adapters/http.py`, `errors.py` | Working |
| Entity resolution | `src/matching/` | Working |
| MCDM ranking | `src/ranking/mcdm.py` | Working |
| Currency normalisation | `src/money.py` | Working |
| Storage + price history | `src/storage/db.py` | Working |
| Configuration | `src/config.py` | Working |
| HTTP API | `src/api/` | Working |
| Showcase dashboard | `web/`, `scripts/build_dashboard.py` | Working (static snapshot) |
| **Recommender (hybrid CF)** | — | **Not started** |
| **Price forecasting** | — | **Not started** (data now available) |

---

## The HTTP API

Previously the only way to see results was a static generated HTML file. A
reviewer could not type their own query, and nothing could integrate with it.

```bash
python -m scripts.serve
```

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness **plus data-quality diagnostics** |
| `GET /config` | effective configuration, secrets redacted |
| `GET /search?q=iphone&price=0.9` | ranked offers under a weight vector |
| `POST /search` | same, JSON body |
| `GET /products` | resolved clusters without ranking |
| `GET /price-history/{source}/{id}` | one item's observed series |
| `GET /tracked-items` | everything with enough history to model |
| `GET /docs` | generated OpenAPI UI |

Ranking runs server-side, so the API and the paper cannot disagree about what
TOPSIS returns. Every search response carries a `diagnostics` block with
per-source counts, cache hits and adapter failures — a degraded result is
visible in the response, not only in the logs.

---

## Results

### Result 1 — Entity resolution ablation

Pairwise precision/recall/F1 against hand-labelled clusters
(`python -m scripts.evaluate_matching`):

| Configuration | Precision | Recall | F1 |
|---|---|---|---|
| Embeddings only | 0.556 | 1.000 | 0.714 |
| Blocking only | 0.833 | 1.000 | 0.909 |
| **Hybrid (ours)** | **1.000** | **1.000** | **1.000** |

**Why embeddings alone fail.** "iPhone 15 128GB Blue" and "iPhone 15 256GB Blue"
are ~0.97 cosine similar but are different products. Embeddings capture topic,
not specification. Hard attributes must be enforced as constraints, not learned.

**Why blocking alone fails.** Rules cannot handle reordered, abbreviated or
paraphrased titles ("Galaxy S24 5G 256 GB Black Samsung").

Three extraction bugs found during development, all preserved as regression tests:

- `WH-1000XM5` and `WH-1000XM4` merged, because the model-number regex silently
  returned `None` on a trailing digit after letters.
- `Onyx Black` and `Black` landed in different blocks, because the marketing
  modifier outranked the base colour.
- `2nd Generation` was extracted as a *model number*. It happened to merge
  correctly, but would have failed on `2nd Gen` vs `3rd Gen`. Generation is now
  its own extractor and its own must-not-link constraint.

> **Caveat that matters more than the number.** F1 = 1.000 is measured on 15
> listings and 7 clusters. That is a smoke test, not a result. See Limitations.

### Result 2 — Normalisation choice governs MCDM responsiveness

**This result was refined after testing it on a second dataset, and the refined
version is the one to present.** Run `python -m scripts.analyse_normalisation`.

The original claim was that textbook (vector-normalised) TOPSIS collapses all
shopper profiles onto one winner while min-max stays responsive. On the
15-listing fixture that is true. On the synthetic market — which has a wider,
more realistic price spread — **it is false**: vector normalisation gives three
distinct winners too.

The claim was not wrong, it was under-specified. Vector normalisation preserves
each criterion's coefficient of variation; min-max equalises the spans. So the
textbook method suppresses whichever criterion happens to vary *least*,
regardless of the weight placed on it — and that only causes a visible collapse
when the CV gap is large enough:

| Dataset | CV(price) | CV(delivery) | Ratio | Vector normalisation |
|---|---|---|---|---|
| Mock fixture | 0.016 | 0.374 | **24×** | **collapses** (1/3 winners) |
| Synthetic market | 0.054 | 0.474 | **8.7×** | stays responsive (3/3) |

Min-max is responsive in **both**. That is the actual argument for making it the
default: its behaviour does not depend on how the criteria happen to be
distributed for a given product.

Do not report "vector normalisation collapses the ranking" as a general result.
Report the mechanism and the condition. It is a stronger, more defensible claim,
and an examiner will probe exactly here.

---

## Bugs found and fixed

These were real defects in the working prototype, all now covered by tests.

**1. The price history was a flat line.**
`collect_prices.py` collected from `MockAdapter`, which reads a *static JSON
file*, so it recorded the identical price every day — 30 rows over 2 days with
zero variance. Running it for six months would have produced a perfectly
horizontal series while the row count grew and made the data look healthy.
Forecasting on a constant is not a hard problem, it is an undefined one.

Fixed by `SyntheticMarketAdapter`, a deterministic market simulator with genuine
price movement, plus `db.series_stats()["flat_series"]` which surfaces
zero-variance series on `/health` so this can never be silent again.

**2. Cross-currency ranking was wrong.**
`Listing.price` is a bare float and TOPSIS compares those floats directly. The
mock sources emit INR; eBay emits USD. With no conversion, a $699 listing scored
as *cheaper* than a ₹66,999 listing for the same product. Invisible while only
the mock adapter was wired up — which is exactly why it needed fixing before the
live adapter went into service. Fixed by `src/money.py`, applied once at the
pipeline boundary. Regression test:
`tests/test_money.py::test_usd_listing_no_longer_beats_inr_on_price`.

**3. The response cache was dead code.**
`db.cache_get` / `cache_put` existed and were never called. The README claimed
responses were cached; every repeated query re-hit the source. Now wired into
`pipeline.fetch()` per adapter, per query.

**4. The eBay adapter had never been executed.**
No API keys on the dev machine, so `search()` and `_to_listing()` were entirely
unverified — the first real request would have been the first test.
`tests/test_ebay_adapter.py` exercises the normalisation logic against a saved
response fixture with no network and no credentials, including missing shipping
data, absent seller feedback, and a malformed delivery date.

**5. Untyped error handling.**
Every adapter failure surfaced as a bare `Exception` caught by a blanket
`except Exception`, so a typo in an API key looked exactly like a transient
network blip and got retried forever. Now `AdapterAuthError` (never retried, and
the adapter is taken out of service), `AdapterRateLimited` (retried, honouring
`Retry-After`), `AdapterUnavailable` (retried with exponential backoff + jitter).

**6. The synthetic market's seller ordering was noise.**
Caught by a test. Giving each seller an independent random walk meant 180 days of
divergence (~35%) swamped the ~10% spread between seller archetypes, so the
discount seller was routinely dearer than the express one. That also destroys the
MCDM demonstration — if "cheap seller" is random, a price-weighted ranking ranks
noise. Fixed with a two-factor model: a market-wide component every seller
shares, plus a tightly-bounded seller deviation. Cross-seller correlation is now
> 0.85, as it is in a real market.

**7. The seller archetypes did not conflict.**
The premium seller had both the fastest delivery *and* the best rating, so two of
three shopper profiles picked the same seller and the weight vector looked
half-dead — an artefact of the fixture, not of TOPSIS. The four archetypes now
each win exactly one criterion. `test_no_seller_wins_every_criterion` enforces it.

---

## The synthetic market

`src/adapters/synthetic_adapter.py`. Four seller archetypes, each a pattern you
can point at in a real marketplace, and each winning exactly one criterion:

| Seller | Price | Delivery | Rating | Wins |
|---|---|---|---|---|
| `sim_discount` | 0.930× | 4–7 days | 4.2 | **price** |
| `sim_warehouse` | 0.972× | 6–9 days | 4.9 | **rating** |
| `sim_standard` | 1.000× | 2–4 days | 4.5 | — |
| `sim_express` | 1.058× | 1–2 days | 4.6 | **delivery** |

Nobody wins all three. That is the point: there is no single "best" offer, so the
user's weights have to decide — which is the project's core argument.

Prices move via a two-factor walk (shared market component + small seller
deviation), a slow depreciation trend, weekly seasonality, persistent sale
events, and stochastic stock-outs.

**Determinism is the key property.** Every value derives from
`blake2b(seed, item_id, date)`, not a live RNG. So:

- a 180-day history can be **backfilled**, and it is the same history you would
  have collected had you been running since February
- forecasting is developable **today** instead of in 30 days
- tests and paper figures are reproducible

```bash
python -m scripts.backfill_prices --days 180
python -m scripts.backfill_prices --purge-flat --dry-run   # audit bad series
```

> **Honesty requirement, non-negotiable.** Simulated rows are tagged
> `is_synthetic = 1` and their sources are prefixed `sim_`. Any evaluation must
> report which rows are simulated. Simulated data validates that the forecasting
> *pipeline* works; it cannot validate that a model predicts the real world. Do
> not let those two claims blur together in the write-up. `/health` returns
> `status: degraded` while every observation is synthetic, specifically so this
> is impossible to forget.

---

## Configuration

Everything is environment-driven via `src/config.py`. Copy `.env.example` to
`.env` and edit; real environment variables always win. `GET /config` shows the
effective settings with secrets redacted.

Keys worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `EMBEDDER_BACKEND` | `auto` | `tfidf` skips the slow torch import entirely |
| `BASE_CURRENCY` | `INR` | everything converts to this before ranking |
| `EBAY_CLIENT_ID` / `_SECRET` | unset | when absent, the synthetic market is used |
| `SIMILARITY_THRESHOLD` | per-backend | override without a code change |
| `CACHE_TTL_SECONDS` | `3600` | response cache lifetime |
| `SYNTHETIC_SEED` | `20260804` | **changing it changes every simulated price** |
| `LOG_FORMAT` | `text` | `json` for one object per line |

---

## Deployment

```bash
docker compose run --rm backfill      # populate price history
docker compose up --build             # API on :8000
docker compose --profile cron up      # + daily collector
```

The image pins Python 3.12 because PyTorch has no 3.14 wheels, so the container
can run the transformer backend even though the dev machine falls back to TF-IDF.
Build with `--build-arg WITH_TRANSFORMERS=1` for the full semantic path (+~2GB).

`.github/workflows/ci.yml` runs the suite on 3.11 and 3.12, asserts the
synthetic history is **not flat**, checks the evaluation scripts and dashboard
still build, and boots the container to hit `/health`.

---

## Limitations

Read this before the review — your supervisor will ask.

1. **The evaluation set is 15 listings and 7 clusters.** F1 = 1.000 on a set this
   small is a smoke test. Before the paper you need **500–1000 hand-labelled
   listing pairs** across several categories. **This is still the single biggest
   gap in the project.**
2. **All price history is currently simulated.** The infrastructure is real and
   the series are realistic, but no forecast accuracy figure measured on this
   data says anything about the market. Get eBay credentials and schedule
   `collect_prices.py`.
3. **The demo runs on TF-IDF, not transformers.** Install
   `sentence-transformers` for the real semantic path; the threshold
   auto-adjusts per backend (`EntityResolver.DEFAULT_THRESHOLD`).
4. **Attribute extraction is English- and electronics-specific.** Apparel (size,
   fit, material) and groceries (weight, pack count) need different rules. Scope
   to one or two categories and say so explicitly.
5. **No recommender yet.** Collaborative filtering needs user-interaction data
   you do not have. Use a public dataset (Amazon Reviews, RetailRocket) rather
   than inventing synthetic users.
6. **No price forecasting yet** — but no longer blocked. 180 days × 28 items of
   varying, autocorrelated series are available now.
7. **The dashboard is still a static snapshot.** `dashboard.html` re-implements
   TOPSIS in JavaScript. It should call `GET /search` instead, so there is one
   implementation of the ranking rather than two.
8. **No remote yet.** The repo exists locally but has never been pushed, so
   there is no off-machine backup. Create a private GitHub repo and push.

---

## What to build next

In dependency order.

1. **Push to a remote.** The repo is initialised and committed, but a repo that
   exists only on one laptop is not a backup.
2. **Expand the labelled dataset** to 500–1000 pairs. Write
   `scripts/label_pairs.py`: blocking-based candidate generation plus a terminal
   labelling UI. Then re-run the ablation with bootstrap confidence intervals,
   and add **external baselines** (fuzzy string match, a supervised pair
   classifier) — currently the ablation only compares the system to crippled
   versions of itself.
3. **Price forecasting.** Baselines first (last-value naive, moving average),
   then SARIMA or gradient boosting on lag features. Report MAE/MAPE *against
   naive*. If you do not beat naive, say so — that is still a result. Deliver a
   "buy now vs. wait" signal with a confidence band.
4. **Recommender.** Popularity → item-item CF → matrix factorisation → hybrid.
   The novelty is the **fusion**: CF behaviour signal + content features from the
   resolver's extracted attributes + the MCDM weight vector as an explicit
   preference prior. That three-way fusion is what makes "hyper-personalised"
   defensible, and it is what ties the existing modules into one contribution
   rather than three separate ones.
5. **Point the dashboard at the API** so the ranking has one implementation.

---

## Data-source policy

Live data comes from official APIs (eBay Browse; Best Buy and Walmart also have
open developer programmes) rather than scraping Amazon or Flipkart, whose terms
prohibit it and whose bot detection would consume the semester. The HTTP client
rate-limits itself, identifies itself with a `User-Agent`, retries with
exponential backoff and jitter, and honours `Retry-After`. All responses are
cached — and that is now true in the code, not just in this file.

---

## Layout

```
src/config.py            environment-driven settings, single source of truth
src/logging_setup.py     structured logging (text or JSON)
src/money.py             currency normalisation
src/pipeline.py          fetch -> normalise -> cache -> resolve -> rank
src/adapters/            base.py  errors.py  http.py  mock_  ebay_  synthetic_
src/matching/            embedder.py  resolver.py
src/ranking/             mcdm.py
src/storage/             db.py  (cache + price history + migrations)
src/api/                 app.py  schemas.py  deps.py

scripts/serve.py                 start the API
scripts/demo.py                  terminal walkthrough
scripts/backfill_prices.py       generate/audit price history
scripts/collect_prices.py        daily snapshot (schedule this)
scripts/evaluate_matching.py     ablation table
scripts/analyse_normalisation.py the refined Result 2
scripts/build_dashboard.py       static dashboard
scripts/update_fx_rates.py       refresh the FX snapshot

tests/                   145 tests across 8 files
data/                    sample_listings.json  fx_rates.json  aggregator.db
Dockerfile  docker-compose.yml  .github/workflows/ci.yml  .env.example

README.md                reference: what exists, results, limitations
PROJECT_NOTES.md         narrative: what was broken and why each fix matters
QUICKSTART.md            step-by-step commands and troubleshooting
```
