# AI-Driven E-Commerce Aggregator and Hyper-Personalized Recommendation Engine

Project Work — CVR College of Engineering, Dept. of CSE (AY 2026-27)

Cross-marketplace product aggregation with domain-agnostic entity resolution,
multi-criteria (TOPSIS) ranking and recommendations, served over an HTTP API with
a live web frontend.

**Status:** aggregation, entity resolution, ranking, buy links and
recommendations are complete and tested (**226 tests**). Price forecasting is not
built yet, but its data infrastructure exists and works.

Products come from a **live catalogue over HTTP with no API key** — 194 products
across 24 categories (clothing, groceries, furniture, beauty, footwear,
jewellery, laptops, phones) with real product images. A fresh clone has a working
multi-category app immediately.

> **Explaining this project to someone?** Read **[PROJECT_NOTES.md](PROJECT_NOTES.md)**
> for the narrative — what was broken, why it mattered, the reasoning behind each
> fix. **[DEMO_SCRIPT.md](DEMO_SCRIPT.md)** is the command-by-command walkthrough
> with what to say and what to do when something breaks. This README is the
> reference.

## See it working

**Windows (cmd.exe):**

```bat
pip install -r requirements.txt
set EMBEDDER_BACKEND=tfidf
python -m scripts.backfill_prices --days 180
python -m scripts.serve
```

Then open **http://127.0.0.1:8000**.

<details>
<summary>Same thing in PowerShell / macOS / Linux</summary>

The only line that differs is the environment variable. `set` is cmd.exe syntax,
`$env:` is PowerShell, `export` is bash — using the wrong one gives you
`'export' is not recognized` or `'set' is not recognized`.

```powershell
# PowerShell
$env:EMBEDDER_BACKEND = "tfidf"
```

```bash
# macOS / Linux
export EMBEDDER_BACKEND=tfidf
```

</details>

One process serves the API *and* the frontend. Four tabs, each mapping to a
pipeline stage:

| Tab | Shows |
|---|---|
| **1 · Search & Rank** | search or browse 24 categories · product images · all three shopper profiles side by side · buy links · "customers also bought" |
| **2 · Entity Resolution** | extracted attributes, block keys, why XM4 ≠ XM5 |
| **3 · Price History** | 180-day series per item, inline SVG chart |
| **4 · System Health** | live diagnostics, including `flat_series` |

Try searching `shirt`, `sofa`, `mascara`, `nike shoes`, `laptop` — or click a
category chip to browse.

The browser does **not** rank anything — every score comes from
`src/ranking/mcdm.py` via `GET /search`. No CDN, no build step, works offline
(falling back to a bundled catalogue if the network is down).

---

## Run it

Windows: double-click **`run.bat`** — installs, tests, generates price history,
runs the demo and evaluations, builds the dashboard, opens it.

Everything individually (set `EMBEDDER_BACKEND` first, see above):

```
python -m pytest tests -q                        226 tests
python -m scripts.backfill_prices --days 180     generate price history
python -m scripts.serve                          API + frontend -> :8000
python -m scripts.demo                           terminal walkthrough
python -m scripts.evaluate_matching              entity-resolution ablation
python -m scripts.analyse_normalisation          the corrected Result 2
python -m scripts.collect_prices                 daily snapshot (schedule this)
python -m scripts.build_dashboard                legacy static page
```

Step-by-step commands and troubleshooting: **QUICKSTART.md**
Demoing it to someone: **DEMO_SCRIPT.md**

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
| **Live product catalogue** | `src/catalogue.py` | **Working** — 194 products, 24 categories |
| **Domain-agnostic attribute extraction** | `src/matching/attributes.py` | **Working** |
| Source adapters | `src/adapters/base.py`, `mock_adapter.py`, `ebay_adapter.py` | Working |
| Synthetic market (seller layer) | `src/adapters/synthetic_adapter.py` | Working |
| HTTP resilience (retry, rate limit) | `src/adapters/http.py`, `errors.py` | Working |
| Entity resolution | `src/matching/` | Working (~92% cluster precision, see below) |
| MCDM ranking | `src/ranking/mcdm.py` | Working |
| **Buy links** | `src/links.py` | **Working** |
| **Recommender** | `src/recommender.py` | **Working** (rules + content, not CF) |
| Currency normalisation | `src/money.py` | Working |
| Storage + price history | `src/storage/db.py` | Working |
| Configuration | `src/config.py` | Working |
| HTTP API | `src/api/` | Working |
| Live frontend | `web/app/` | Working |
| Static snapshot dashboard | `web/template.html`, `scripts/build_dashboard.py` | Working (legacy) |
| **Price forecasting** | — | **Not started** (data available) |

---

## Where products come from

**An aggregator has no catalogue of its own.** Amazon has a product catalogue
because Amazon *is* a retailer — sellers list into its taxonomy. An aggregator
(Google Shopping, PriceGrabber, Junglee) has none: it queries retailer APIs at
request time and resolves whatever comes back.

So the product list is a **remote, replaceable resource**, never a file to grow:

| Source | What it gives | Needs a key? |
|---|---|---|
| `DummyJsonCatalogue` *(default)* | 194 products, 24 categories, real images | **No** |
| **`OpenPricesCatalogue`** | **4.4M real products with real shop prices** | **No** |
| `FileCatalogue` | `data/catalogue.json`, ~44 products | No — offline fallback only |
| eBay Browse API | real sellers, real prices, real product URLs | Yes (free, needs approval) |

Set with `CATALOGUE_SOURCE=auto|dummyjson|file`. A fallback logs a **warning**,
never passes silently — serving 44 local products while appearing to serve 194
live ones is exactly the kind of quiet degradation this project has been bitten
by before.

### Real prices with no API key — Open Prices

```
set SOURCE=openprices
python -m scripts.serve
```

`src/openprices.py` uses **Open Food Facts** (product database) and
**Open Prices** (its crowd-sourced price layer): 286,000+ real price observations
from real named shops, each dated. No key, no registration, no approval queue.

Searching `nutella` returns genuine price comparison:

| Shop | Price | Converted |
|---|---|---|
| Super U, Paris | €2.76 | ₹303 |
| Auchan Supermarché, Grenoble | €2.77 | ₹305 |
| Colruyt, Laneuveville | €3.52 | ₹387 |
| Auchan, Nancy | €3.65 | ₹401 |

Nobody invented those numbers. **This is also the first source that makes
`real_observations` on `/health` non-zero**, because every observation carries the
date it was seen — so `price_history` can hold real market prices instead of
simulated ones.

**Honest limits, and they are real:**

- **Groceries and household goods only.** No phones, no laptops — Open Food Facts
  is a food database.
- **Mostly European shops, priced in EUR.** Converted by `src/money.py`, so a
  stale FX snapshot shifts every price. `/health` reports the snapshot's age.
- **Crowd-sourced**, so coverage is uneven and a price may be months old. The
  observation date is carried through and shown, not hidden.
- **Physical shops have no delivery time and no seller rating.** Those are left
  `None` and imputed equally, so for this source the MCDM ranks on price alone.
  That is the correct answer for a supermarket shelf — but it means the
  three-profile demo is less interesting here than on the simulated market.

So: use `SOURCE=openprices` when the question is *"are the prices real?"*, and
`SOURCE=synthetic` when it is *"does multi-criteria ranking work?"*. Both are
wired; neither pretends to be the other.

> **A bug this source found.** A Danish shop's price arrived in **DKK**, which had
> no entry in the FX table — so it passed through unconverted and a 25 DKK jar was
> compared as 25 INR, winning the price ranking outright. Exactly the bug
> `src/money.py` was written to prevent, reintroduced by an *incomplete lookup
> table* rather than by missing code. The table now carries 44 currencies, and an
> unconvertible price is tagged `_price_not_comparable` and counted into
> `diagnostics.prices_not_comparable` — because a wrong ranking caused by a
> missing rate looks exactly like a correct one.

**Product and seller are separate dimensions.** The catalogue supplies products;
the adapter layer supplies offers — who sells it, at what price, how fast, how
well rated. That separation is what lets the same product list be served by the
simulated marketplace today and by live eBay sellers tomorrow with no change to
the catalogue code.

---

## Buy links — and one thing this project will not do

Two kinds of link, never conflated:

| Kind | Source | Carries a price? |
|---|---|---|
| **Verified offer** | the source itself (`Listing.url`, e.g. eBay) | **Yes** — the exact offer shown |
| **Retailer search** | constructed (`amazon.in/s?k=…`) | **No** |

`https://www.amazon.in/s?k=nike+air+jordan+1` is just a URL. Building one is not
scraping and breaks no terms of service. It lands the user on the retailer's real
results for that product.

**What it will not do is display "Amazon: ₹70,000".** Amazon's terms prohibit
scraping and its Product Advertising API requires an approved Associates account
with qualifying sales; Flipkart's affiliate programme is closed to new signups.
A price attributed to either would therefore be **invented — a fabricated record
of a real company's pricing.** That is indefensible in a review and actively
harmful if the app is deployed publicly. Every link carries its `kind`, and
`test_retailer_links_never_carry_a_price` enforces the boundary, because a rule
stated only in a comment is one edit from being crossed silently.

Verified links are empty until a live source is configured — the simulated
sellers have no web pages, and inventing URLs for them would be worse than having
none.

---

## Recommendations

`GET /recommendations?title=…`. Three signals, blended:

| Signal | Weight | What it is |
|---|---|---|
| Complement | 1.00 | cross-category rules — laptop → bag, dress → shoes |
| Substitute | 0.55 | same category, comparable price band |
| Affinity | 0.35 | shared brand or tags |
| *Co-occurrence* | *1.20* | *reserved: real behaviour outranks every rule* |

Complements are defined per **category pair**, not per product, so a new product
inherits them for free — the same principle as the attribute extractor.

**Collaborative filtering is deliberately absent, not faked.** CF needs an
interaction matrix and this project has no users. Inventing random purchases
produces random recommendations that are indistinguishable from a working
recommender until someone reads the output carefully. `score_from_cooccurrence`
is the wired slot where a real dataset (Amazon Reviews, RetailRocket) enters, and
the blend already weights it above every rule.

Say this plainly in the review: **this is a content-and-rules recommender, not
collaborative filtering.** It cannot discover the non-obvious pairings CF finds
from real behaviour.

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
| `GET /search?q=shirt&price=0.9` | ranked offers, per-profile winners, images, buy links |
| `GET /search?category=womens-dresses` | browse instead of search |
| `POST /search` | same, JSON body |
| `GET /categories` | browsable categories with product counts |
| `GET /recommendations?title=…` | "customers also bought" |
| `GET /products` | resolved clusters + **extracted attributes and block keys** |
| `GET /price-history/{source}/{id}` | one item's observed series |
| `GET /tracked-items` | everything with enough history to model |
| `GET /docs` | generated OpenAPI UI |
| `GET /` | the frontend |

Ranking runs server-side, so the API and the paper cannot disagree about what
TOPSIS returns. Every search response carries a `diagnostics` block with
per-source counts, cache hits and adapter failures — a degraded result is
visible in the response, not only in the logs.

---

## Results

### Result 1 — Entity resolution ablation

Pairwise precision/recall/F1 against hand-labelled clusters
(`python -m scripts.evaluate_matching`). The resolver has **three** separable
components, so the ablation isolates each:

| Configuration | Precision | Recall | F1 |
|---|---|---|---|
| Embeddings only | 0.556 | 1.000 | 0.714 |
| Blocking only | 0.556 | 1.000 | 0.714 |
| Blocking + embeddings | 0.556 | 1.000 | 0.714 |
| **Full hybrid (+ specification veto)** | **1.000** | **1.000** | **1.000** |

**The specification veto contributes the entire +0.286 F1.** Blocking and
embeddings together add nothing over either alone on this set — which is a more
precise finding than the earlier three-row version of this table, and it
correctly attributes the credit.

**Why embeddings alone fail.** "iPhone 15 128GB Blue" and "iPhone 15 256GB Blue"
are near-identical as text. Embeddings capture topic, not specification. Hard
attributes must be enforced as constraints, not learned.

**Why blocking alone fails.** Rules cannot handle reordered, abbreviated or
paraphrased titles ("Galaxy S24 5G 256 GB Black Samsung").

**Why blocking is deliberately weak here.** Blocking partitions on
`brand + colour` only — no quantities. Block keys must match *exactly*, so every
field added multiplies the chance that a listing which merely *omits* that field
is exiled into a singleton block, where semantic clustering never runs and recall
silently collapses. All numeric precision therefore lives in the veto, which stays
silent when a spec is missing. That trade is why the earlier version of this table
showed blocking alone at 0.909 and this one shows 0.714 — the strength moved, it
did not vanish.

The script also prints exactly which pairs the veto stopped and how similar their
text was. The XM4/XM5 pair sits at 0.625 cosine similarity — it *would* have
merged on semantics alone.

### Result 1b — precision on the live catalogue (the honest number)

F1 = 1.000 above is on 15 hand-labelled listings. On the **live 194-product
catalogue** across 24 categories, measured by counting clusters holding more
offers than there are sellers:

| | Value |
|---|---|
| Products checked | ~100 across 9 categories |
| Over-merged clusters | **8** |
| Approximate cluster precision | **~92%** |

Down from 10 after two targeted fixes:

- **Variant designators were being discarded.** `iPhone 6` and `iPhone X` both
  reduced to `{iphone}`, because the extractor drops tokens under three
  characters and bare digits — exactly the tokens that separate one model in a
  line from the next. Galaxy S7/S8/S10 and Realme C35 now separate.
- **Product lines named by words had nothing to veto them.** Rolex Datejust,
  Cellini and Submariner share a brand and a string shape, and TF-IDF character
  n-grams scored them as near-identical. A disjoint-product-name check fixed most
  of it, stripping the brand first — it is the token those titles share, and
  leaving it in makes every product from one maker look related.

A first attempt at the variant rule **dropped gold-set recall from 1.000 to
0.900** by splitting one real product: retailers write both "Nord CE 4" and
"Nord CE4", which tokenise to `{4}` and `{ce4}`. Comparing from the first digit
onwards makes those agree while keeping `s7`/`s10` and `6`/`16` apart.

**What remains is beyond string heuristics**, and is reported rather than chased:

| Still merging | Why |
|---|---|
| `iPhone X` / `Realme XT` | letter-only model names — no digit to compare |
| `Beef Steak` / `Fish Steak` | differ by one word while sharing the noun |
| `Cat Food` / `Dog Food` | same |

Every further rule attempted broke the gold set. The principled fix is the
**transformer embedding backend**, which is a configuration change
(`EMBEDDER_BACKEND=transformer`), not new code — TF-IDF character n-grams
fundamentally cannot tell "Beef" from "Fish" when the rest of the string matches.

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
5. **The recommender is rules-and-content, not collaborative filtering.** It
   works and its suggestions are relevant, but it cannot find the non-obvious
   pairings CF discovers from real behaviour. Upgrade with a public interaction
   dataset (Amazon Reviews, RetailRocket) — the slot is wired.
6. **No price forecasting yet** — but no longer blocked. 180 days × 28 items of
   varying, autocorrelated series are available now.
9. **Entity resolution is ~92% precise on the live catalogue**, not 100%. See
   Result 1b. The fix is the transformer backend, not more string rules.
10. **Catalogue prices are reference prices, not retailer quotes.** The live
    catalogue is a public demo API: its products and images are real data over
    real HTTP, but its prices are not quotes from a shop. `is_reference_price`
    marks this and it stays labelled downstream.
7. **No remote yet.** The repo exists locally but has never been pushed, so
   there is no off-machine backup. Create a private GitHub repo and push.
8. **The legacy `dashboard.html` still re-implements TOPSIS in JavaScript.** The
   live frontend at `web/app/` calls `GET /search` and does not, so the ranking
   has one implementation there. The old static page is kept only because it
   needs no server; delete it once nobody depends on that.

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
4. **Upgrade the recommender to real collaborative filtering.** Load a public
   interaction dataset into `score_from_cooccurrence`, then evaluate
   leave-one-out with Recall@K and NDCG@K against the current rules baseline.
   The novelty is the **fusion**: CF behaviour + content features from the
   resolver's extracted attributes + the MCDM weight vector as an explicit
   preference prior. That three-way fusion is what makes "hyper-personalised"
   defensible, and what ties the modules into one contribution rather than three.
5. **Try `EMBEDDER_BACKEND=transformer`** and re-measure Result 1b. If it fixes
   the Beef/Fish class of error, that is a clean result: a table of cluster
   precision by backend.
6. **Get eBay credentials in** for real seller prices and verified buy links.
7. **Delete the legacy `dashboard.html`** once nobody depends on a server-free page.

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
src/catalogue.py         the PRODUCT dimension: live API + offline fallback
src/links.py             buy links: verified offers vs retailer search
src/recommender.py       complements + substitutes + affinity
src/logging_setup.py     structured logging (text or JSON)
src/money.py             currency normalisation
src/pipeline.py          fetch -> normalise -> cache -> resolve -> rank
src/adapters/            base.py  errors.py  http.py  mock_  ebay_  synthetic_
src/matching/            attributes.py  embedder.py  resolver.py
src/ranking/             mcdm.py
src/storage/             db.py  (cache + price history + migrations)
src/api/                 app.py  schemas.py  deps.py
web/app/                 index.html  style.css  app.js   <- the live frontend

scripts/serve.py                 start the API
scripts/demo.py                  terminal walkthrough
scripts/backfill_prices.py       generate/audit price history
scripts/collect_prices.py        daily snapshot (schedule this)
scripts/evaluate_matching.py     ablation table
scripts/analyse_normalisation.py the refined Result 2
scripts/build_dashboard.py       static dashboard
scripts/update_fx_rates.py       refresh the FX snapshot

tests/                   226 tests across 10 files
data/                    catalogue.json (offline fallback)  fx_rates.json
                         sample_listings.json (gold eval set)  aggregator.db
Dockerfile  docker-compose.yml  .github/workflows/ci.yml  .env.example

README.md                reference: what exists, results, limitations
PROJECT_NOTES.md         narrative: what was broken and why each fix matters
DEMO_SCRIPT.md           command-by-command demo, with what to say
QUICKSTART.md            step-by-step commands and troubleshooting
```
