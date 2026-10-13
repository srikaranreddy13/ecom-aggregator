# Project Notes — how to explain this work

A walkthrough you can talk from. Ordered as a story, not as a file listing.

**The one-sentence version:** the prototype's three algorithms worked, but the
engineering around them was quietly broken in ways that would have wrecked the
next phase — so we fixed the foundations, wrapped the system in a real API, and
built the test suite that proves the fixes hold.

**The numbers:** 11 tests → **221**. Price history: 30 rows over 2 days with zero
price movement → **4,842 observations over 180 days** that actually move. Plus one
of our published results turned out to be over-general, and we corrected it.

---

## Part 0 — Where the project stood

Say this first, because it frames everything else.

The prototype had three working algorithms:

1. **Ingestion** — several marketplaces behind one common interface
2. **Entity resolution** — deciding that "iPhone 15 128GB Blue" from seller A and
   "APPLE iPhone 15 128 GB Blue Smartphone" from seller B are the *same product*
3. **MCDM ranking** — given one product with several offers, rank them according
   to what *this* shopper cares about (price vs. speed vs. seller trust)

Those three were genuinely good and we did not rewrite them.

But the project promises three things in its title: aggregation ✅,
**recommendation** ❌, **price intelligence** ❌. Two of the three headline modules
did not exist. So the question was: what has to be true before we can build them?

That question is what drove everything below.

---

## Part 1 — The bug that would have cost us the semester

**This is the most important thing to explain. Lead with it.**

### What we found

Price forecasting needs price *history*. You cannot scrape the past — if you
aren't recording prices today, that data is gone forever. So the project had a
`collect_prices.py` script meant to run daily and snapshot every price.

It was running. The database had rows in it. It looked healthy.

But it collected from `MockAdapter` — and `MockAdapter` reads a **static JSON
file**. So every single day it recorded *the exact same price*.

### Why this was so dangerous

Look at what the table showed: 30 rows, 2 days, growing. Every signal said
"working". The row count going up is exactly what you'd check.

But the *variance* was zero. Every series was a horizontal line.

> **The line to use when explaining this:** "Forecasting a constant isn't a hard
> problem — it's an undefined one. There is nothing to learn. We could have run
> that collector for six months, sat down to build the forecasting model, and
> discovered on that day that we had six months of nothing."

That is the worst class of bug: **silent, and only discovered at the moment you
need the thing it destroyed.** No error message, no crash, no failing test.

### What we did about it

Two parts, and the second is the more important one.

**(a) We built a market simulator that produces real price movement.**
`src/adapters/synthetic_adapter.py`. Four seller archetypes with prices that
move day to day — depreciation trend, weekly promotion cycles, sale events, and
random drift.

**(b) We made the failure impossible to hide again.**
`db.series_stats()` now computes `flat_series` — a count of how many price series
have more than one observation but zero variance. It is reported on the
`/health` endpoint and asserted in CI.

Point (b) is the real lesson. Fixing a bug is ordinary; **making that class of
bug loud** is engineering. If a mentee takes one thing from this project, it
should be that.

### The clever bit: determinism

The simulator derives every price from `hash(seed, item_id, date)` — specifically
`blake2b`, not Python's built-in `hash()`.

Why that matters: `blake2b` gives the *same answer every time, in every process,
forever*. Python's `hash()` on strings is randomly salted per run, so it would
give different prices tomorrow.

Because it's deterministic, we can ask "what was this price on 6 February?" and
get the answer it *would* have given had we been collecting since February. So:

```bash
python -m scripts.backfill_prices --days 180
```

**We backfilled 180 days of history instantly.** Forecasting is developable
*today* instead of after a 30-day wait. Tests and paper figures are reproducible.

### The honesty requirement — do not skip this when presenting

Simulated data is a scaffold, not evidence. So we enforced separation:

- every simulated row is tagged `is_synthetic = 1` in the database
- simulated sources are named `sim_discount`, `sim_express`, etc.
- `/health` returns **`status: degraded`** while all observations are synthetic,
  with the warning: *"any forecast accuracy figure measures the simulator, not
  the market"*

> **Say this out loud in the review, before anyone asks:** "Simulated data proves
> the forecasting *pipeline* works. It says nothing about whether the model
> predicts the real world. Those are two different claims and we've made it
> structurally hard to confuse them."

Volunteering a limitation is far stronger than being caught on it.

---

## Part 2 — A bug that was invisible by luck

### What we found

`Listing.price` is a plain number. The ranking algorithm compares those numbers
directly. And the mock sources return Indian rupees while the eBay API returns
US dollars.

So for the same physical phone:

- ₹66,999 from a local seller
- $699 from eBay

The ranking compared `699 < 66999` and concluded the eBay listing was about
**100× cheaper**. Any price-weighted ranking mixing a live source with a local
one was simply wrong.

### Why it was invisible

Only the mock adapter was actually wired up, and everything it returned was in
rupees. **The bug was dormant purely because the live source wasn't switched on
yet.**

> **The framing to use:** "This is why you fix a bug like this *before* the live
> adapter goes into service, not after. The day we turned on eBay credentials,
> every ranking would silently have become wrong — and 'silently' is the problem
> word. Nothing would have crashed."

### What we did

`src/money.py`. All prices convert to one currency at the pipeline boundary,
*once*, before anything downstream sees them.

Design decisions worth explaining, because each has a reason a mentee should
understand:

- **Conversion is non-destructive.** The original price and currency are kept, so
  the UI can display "$699 (converted)" and the conversion is auditable. Never
  throw away the input.
- **Exchange rates are a pinned file, not a live API call.** Reason: if rates
  move between two runs, the same query returns a different winner and no result
  in the paper can be reproduced. Reproducibility beats freshness here. It also
  removes a network dependency that could fail on demo day.
- **A stale rate file is visible, not silent.** `/health` reports the snapshot's
  age in days. (Notice the pattern repeating: don't just handle the problem —
  *surface* it.)
- **An unknown currency doesn't crash and doesn't vanish.** It's logged and the
  listing passes through unconverted. Crashing lets one bad row kill an entire
  query; dropping it silently shrinks a product's offer list with no trace.

The regression test is named so it explains itself:
`test_usd_listing_no_longer_beats_inr_on_price`.

---

## Part 3 — Three smaller findings

Quick to explain, and together they make a point about documentation drift.

### The cache was never called

`cache_get` and `cache_put` were fully written, correct, and **called by
absolutely nothing**. The README stated "all responses are cached so repeated
queries do not re-hit the source." That was false. Every repeated query hit the
source again.

> **The lesson:** a README is a *claim*, not evidence. Written code is not
> running code. Now it's wired into `pipeline.fetch()` — and there's a test
> asserting the adapter is called once, not twice, for a repeated query. The test
> is the evidence.

### The eBay adapter had never once been executed

No API keys on the dev machine, so `search()` and the response-parsing logic had
literally never run. The first real API request would have been the first test.

Fixed with a **fixture**: a saved copy of a real API response shape, so the
parsing logic runs with no network and no credentials. And we deliberately
included the awkward cases real APIs actually return:

- a listing with no shipping information
- a seller with no feedback score
- a *malformed* delivery date
- an item with no price block at all

One nice detail to mention: a seller with no feedback maps to `None`, **not
zero**. Zero would mean "terrible seller" and unfairly sink every new seller.
Unknown and bad are different things.

### Error handling couldn't tell disasters from hiccups

Every failure was caught by one blanket `except Exception`. So a **typo in an API
key** looked exactly like a **momentary network blip** — and got retried forever.

Now there are typed errors, and each implies a different correct response:

| Error | Meaning | Correct action |
|---|---|---|
| `AdapterAuthError` | credentials wrong | **never retry** — take the source out of service |
| `AdapterRateLimited` | going too fast | retry, and obey the server's `Retry-After` |
| `AdapterUnavailable` | timeout / server error | retry with exponential backoff |

Plus one policy rule: **one dead source degrades the result, it never fails the
query.** If eBay is down you still get results from everywhere else, and the
response carries a `diagnostics` block saying which source failed — so a degraded
answer is *visible to the caller*, not buried in a log file.

> **Worth saying:** "Error handling is a claim like any other. An untested claim
> about error handling is usually false — so we wrote tests that assert a 401 is
> retried *zero* times and a 503 is retried exactly three."

---

## Part 4 — Two bugs our own tests caught in our own new code

**Include this. It's the most credible part of the whole story**, because it shows
the tests are doing real work rather than confirming what we already believed.

### Bug 1: the simulator's sellers were behaving impossibly

We gave each seller their own independent random price walk. Seemed reasonable.

A test failed: the *discount* seller was more expensive than the *express*
seller.

**Why:** over 180 days, independent random walks drift apart by up to ~35%. The
designed price gap between seller types was only ~10%. So the random drift
completely swamped the seller identity. "Cheap seller" had become random noise.

**Why that's a modelling error, not just a bad number:** real competing sellers
of the same product face the same wholesale cost and the same demand, so their
prices **move together**. What differs between them is a persistent margin plus a
small individual wobble. Independent walks describe a market that cannot exist.

And it would have destroyed the MCDM demonstration — if "cheapest seller" is
random, then a price-weighted ranking is ranking noise.

**The fix** is a standard two-factor model:

```
price = base × market_factor × seller_factor × trend × seasonality × sale
                 ^^^^^^^^^^^   ^^^^^^^^^^^^^
                 shared by     small, tightly
                 all sellers   bounded, per-seller
```

Now cross-seller correlation is **above 0.85**, as it is in a real market, and
there's a test asserting exactly that.

### Bug 2: our seller archetypes made the ranking demo impossible

The premium seller had **both** the fastest delivery **and** the best rating. So
the "I'm in a hurry" shopper and the "I want a trusted seller" shopper both
picked the same seller. Two of three profiles gave the same answer, and the
weight slider looked half-broken.

**The insight worth explaining:** that was an artefact of our *test data*, not of
the algorithm. TOPSIS was working perfectly. If one seller is best at everything,
there *is* a single objectively best offer, and no weighting can change it.

But that undermines the project's entire premise. So the four archetypes were
redesigned so each wins exactly one criterion:

| Seller | Price | Delivery | Rating | Wins |
|---|---|---|---|---|
| `sim_discount` | cheapest | 4–7 days | 4.2 | **price** |
| `sim_warehouse` | cheap | 6–9 days | 4.9 | **rating** |
| `sim_standard` | mid | 2–4 days | 4.5 | — |
| `sim_express` | dearest | 1–2 days | 4.6 | **delivery** |

Every one of those is a pattern you can point at on a real marketplace — the
big reputable warehouse with excellent feedback but slow economy shipping is a
completely ordinary thing.

**Nobody wins all three. That is the whole argument of the project:** there is no
single "best" offer, so the user's priorities have to decide.

There's now a test called `test_no_seller_wins_every_criterion` that fails if
anyone ever "improves" the fixture back into a state where the premise collapses.

---

## Part 5 — We corrected one of our own published results

**Do not hide this. Presented properly it is a strength, and it is also the thing
most likely to be probed in a viva.**

### The original claim

The README stated, as a general finding: textbook TOPSIS (which uses *vector*
normalisation) collapses every shopper profile onto the same winner, while our
min-max normalisation keeps the weights responsive.

On the 15-listing fixture, that is exactly what happens.

### What we found

We ran the same comparison on the synthetic market, which has a wider and more
realistic price spread. **Vector normalisation gave three distinct winners too.**
The claim did not hold.

### Why — the actual mechanism

This is the part to understand properly, because it's the real result.

- **Vector normalisation** divides each criterion by its column magnitude. This
  *preserves* how much each criterion varies relative to its own average (its
  coefficient of variation, CV).
- **Min-max normalisation** rescales every criterion to the same 0–1 span. This
  *equalises* the variation.

Consequence: vector normalisation suppresses whichever criterion happens to vary
*least* — **no matter how much weight the user puts on it.** If price barely
varies between sellers while delivery time varies hugely, then a weight of 0.8 on
price loses to a weight of 0.1 on delivery. The user's slider stops working.

But that only becomes *visible as a collapse* when the variation gap is large
enough:

| Dataset | CV(price) | CV(delivery) | Ratio | Vector normalisation |
|---|---|---|---|---|
| Mock fixture | 0.016 | 0.374 | **24×** | **collapses** (1 winner from 3 profiles) |
| Synthetic market | 0.054 | 0.474 | **8.7×** | stays responsive (3 winners) |

At 24× it collapses. At 8.7× it doesn't.

### The corrected claim, and why it's better

> "Our original claim wasn't wrong — it was **under-specified**. We reported a
> conclusion when we should have reported a mechanism and a condition.
>
> The correct statement: vector normalisation's responsiveness depends on the
> ratio of coefficients of variation between criteria. Min-max is responsive in
> **both** datasets — and *that* is the actual argument for making it the default.
> Its behaviour doesn't depend on how the criteria happen to be distributed for
> whatever product the user searched for.
>
> A real marketplace sits at whatever CV ratio it sits at, and we don't control
> that. So we want the method whose behaviour doesn't depend on it."

That is a stronger and more defensible claim than the original, and it's
reproducible: `python -m scripts.analyse_normalisation`.

> **The methodological lesson for a mentee:** we only found this because we tested
> the claim on a *second* dataset. A result verified on one dataset — especially a
> 15-row hand-built one — is a hypothesis. Our own fixture had been quietly
> flattering us.

---

## Part 6 — Turning it into a real system

Everything so far was correctness. This part is about it being a *product*.

### It's a service now, not a static file

Before: the only way to see results was `build_dashboard.py`, which baked a
snapshot into an HTML file. Fine for a screenshot, useless otherwise — a reviewer
couldn't type their own query, and nothing could integrate with it.

Now there's an HTTP API (`python -m scripts.serve`):

| Endpoint | What it's for |
|---|---|
| `GET /health` | is it alive, **and is the data any good** |
| `GET /search?q=iphone&price=0.9` | ranked offers under a weight vector |
| `GET /products` | resolved clusters, ranking off — shows entity resolution alone |
| `GET /price-history/{source}/{id}` | one item's observed price series |
| `GET /tracked-items` | everything with enough history to model |
| `GET /docs` | interactive API documentation, auto-generated |
| `GET /config` | effective settings, with secrets redacted |

Two design points worth mentioning:

- **Ranking runs server-side.** The old dashboard reimplemented TOPSIS in
  JavaScript — two implementations of one algorithm, which can disagree. Now
  there's one.
- **The API doesn't write price history.** That's deliberate: if every browser
  request recorded a price, popular products would appear to have more history
  than quiet ones — the series would be sampled by *user activity* instead of by
  *time*. Collection is a scheduled job. Sampling bias is a subtle thing to have
  spotted; it's worth saying you spotted it.

### Live demo you can do in the review

Same product, two different shoppers:

```
/search?q=iphone&price=0.9&delivery_days=0.05&seller_rating=0.05   -> sim_discount
/search?q=iphone&price=0.05&delivery_days=0.05&seller_rating=0.9   -> sim_warehouse
```

**Different winner, same query.** That's the whole thesis in one click.

### Configuration in one place

`src/config.py`. Every tunable reads from the environment *here and nowhere else*.
Modules never call `os.getenv` themselves.

Why: one place to look when something behaves differently on your laptop than in
the container. `GET /config` shows the effective settings with secrets redacted —
which answers "why is it different on your machine?" in a single request.

Also: `Settings` is **frozen** (immutable). Config shouldn't change at runtime.
A test tried to monkey-patch it, failed, and we fixed the *test* rather than
unfreezing the config — the design was right.

### Logging instead of print

`print()` inside a web request goes nowhere useful. `LOG_FORMAT=json` emits one
JSON object per line, which is what any log aggregator expects to ingest.

### Health checks that report data quality, not just liveness

This is the piece to be proudest of. Most health endpoints answer "is the process
running?" — which is nearly useless, because the process was running fine the
whole time the price history was garbage.

Ours reports the failures this project can actually *have*:

```json
{
  "status": "degraded",
  "price_history": { "observations": 4842, "distinct_days": 180,
                     "real_observations": 0, "flat_series": 0 },
  "warnings": ["all price observations are synthetic -- any forecast accuracy
                figure measures the simulator, not the market"]
}
```

`flat_series: 0` is the guard against the Part 1 bug ever recurring silently.

### Containers and CI

- **Dockerfile** — pinned to Python 3.12 (PyTorch has no 3.14 wheels, so 3.14
  can't run the transformer backend). Runs as a non-root user.
- **docker-compose** — API + a backfill job + an optional daily collector. The
  database is a named volume, so `docker compose down` doesn't destroy history.
- **GitHub Actions** — runs 221 tests on two Python versions, **asserts the
  synthetic history is not flat**, checks the evaluation scripts and dashboard
  still build, then boots the container and hits `/health`.

That CI assertion is the point: the bug from Part 1 is now something the
*repository itself* refuses to let happen again.

---

## Part 7 — The tests

11 → 197, in 10 files.

| File | Covers |
|---|---|
| `test_pipeline.py` | the original 11 — attribute extraction, resolution, MCDM |
| `test_money.py` | currency conversion, including the exact original bug |
| `test_synthetic_adapter.py` | determinism, variance, autocorrelation, market shape |
| `test_storage.py` | cache, price history, schema migration |
| `test_http_resilience.py` | retry policy, backoff, rate limiting |
| `test_pipeline_integration.py` | caching, currency, failure policy end-to-end |
| `test_ebay_adapter.py` | the previously-unexecuted live adapter |
| `test_api.py` | HTTP contract and input validation |
| `test_frontend.py` | static mount, no-CDN rule, the click-swallowing regression |
| `test_catalogue_links_recommender.py` | catalogue search, the never-claim-a-price boundary, recommendation relevance |

Things worth pointing out about *how* they're written:

- **Tests are named after the bug they prevent.**
  `test_backfill_across_dates_keeps_every_day` documents a real failure. A test
  name is documentation that cannot go stale — if the behaviour changes, the test
  fails.
- **Tests assert properties, not just values.** `test_series_is_autocorrelated_not_white_noise`
  checks that consecutive days are closer than distant days. That's a *statistical
  property* of a realistic price series, not a hard-coded number.
- **Tests use their own throwaway database.** Never the project's real one — that
  would pollute price history, the one asset you can't regenerate.
- **Some tests protect the premise, not the code.**
  `test_no_seller_wins_every_criterion` fails if anyone makes the test fixture
  degenerate. That's an unusual and useful kind of test.

---

## Part 8 — The live frontend, and a bug that endpoint tests could never catch

`web/app/`. Four tabs, one per pipeline stage, served by the same process as the
API so there is one thing to start.

The design rule: **the browser does not rank anything.** Every score comes from
`GET /search`, i.e. from `src/ranking/mcdm.py`. The old `dashboard.html`
re-implemented TOPSIS in JavaScript — two implementations of one algorithm that
could silently disagree, and the JavaScript one was what people actually looked
at. There's a test (`test_frontend_does_not_reimplement_ranking`) that fails if
ranking logic leaks back into the frontend.

It also has **no CDN and no build step**, because the project has to run on a lab
machine with no internet. A CDN `<script>` works on the dev laptop and fails
silently in the room where it matters. That's asserted too.

### The bug worth telling them about

Every endpoint test passed. All routes returned 200. The page screenshotted
perfectly. **And the app was completely unusable.**

The modal dialog is hidden with the HTML `hidden` attribute. But the CSS had:

```css
.modal { position: fixed; inset: 0; display: grid; background: rgba(0,0,0,.65); }
```

A class selector beats the browser's built-in `[hidden] { display: none }`. So the
"hidden" modal stayed laid out — a full-viewport fixed overlay sitting on top of
everything, dimming the page and swallowing every single click.

It was found by **driving the real browser**, where the first attempted click
timed out with `<div hidden id="modal"> intercepts pointer events`. No API test
could have found it — the API was perfect. The fix is one line:

```css
.modal[hidden] { display: none; }
```

> **The lesson, and it generalises past CSS:** a test that exercises the layer
> below can pass completely while the thing the user touches is broken. "All the
> endpoints return 200" is not "the app works." At some point you have to open
> it, click it, and look.

That's the same lesson as Part 1 in a different costume: the row count was going
up, and the data was worthless. Watch the thing you actually care about, not the
proxy that's easy to measure.

---

## Part 9 — The question that changed the architecture

**This is the best part of the story to tell, because the redirection came from
asking the right question rather than from writing more code.**

The project had a hand-written JSON file of products. The obvious next step was to
keep adding to it — more phones, then laptops, then clothing. The right question
was: *why are we typing products into a file at all? Amazon has everything.*

### The answer: an aggregator has no catalogue

Amazon has a product catalogue because Amazon **is a retailer** — sellers list
into its taxonomy. An aggregator — Google Shopping, PriceGrabber, Junglee — has
**none**. It takes your query, asks retailer APIs live, and resolves whatever
comes back.

So growing the JSON file was not a small version of the real thing. It was a
different and unwinnable thing: every domain a user might search would need
someone to type it in first, and the file would never be complete.

> **The line to use:** "We deleted the product list as a source of truth. The
> catalogue is now a remote resource with a local fallback — 194 products across
> 24 categories, fetched over HTTP, no API key. Clothing, groceries, furniture,
> beauty, footwear, jewellery. With real product images."

### What that changed in the code

It also killed a design I was halfway through writing. I had started a
`categories.py` with keyword lists and per-category attribute rules — phones have
storage, appliances have wattage, clothes have sizes. That has the same flaw:
a new branch for every domain, forever, and until someone writes it that domain
resolves badly and silently.

The replacement is one universal observation:

> **A quantity with a unit is a hard specification, in every domain.**

| Domain | Title | Extracted |
|---|---|---|
| electronics | iPhone 15 **256GB** | `256gb` |
| appliance | Mixer **750W** | `750w` |
| grocery | Shampoo **500ml** | `0.5l` |
| clothing | Shirt Navy **Size 42** | `size 42` |
| TV | Bravia **55 inch** | `55in` |
| AC | Voltas **1.5 Ton 3 Star** | `1.5ton · 3star` |

One extractor, no taxonomy, no category branching anywhere. **A domain nobody
anticipated resolves correctly the first time it appears.**

### And it fixed a bug nobody had noticed

A laptop title carries two `GB` figures: `"ThinkPad E14 16GB RAM 512GB SSD"`. The
old extractor scanned for the first plausible number and returned **16** — the
RAM — as the storage size. Two laptops differing only in disk size shared a block
key on the wrong attribute.

Comparing the **maximum per unit** fixes all three cases, and nothing has to know
which number means RAM:

| | Result |
|---|---|
| `128` vs `256` | different ✓ |
| `{16, 512}` vs `{16, 1024}` | different ✓ |
| `{128}` vs `{6, 128}` | **same** ✓ — omission is not evidence of a difference |

Set equality fails the third case; set intersection fails the second. The maximum
gets all three.

### Two details only realistic data revealed

- Apple sells the **"13-inch" MacBook Air whose panel is 13.6"**, and retailers
  use both numbers for the same laptop. Exact matching split one product into two,
  so screen size and battery compare with a relative tolerance while everything
  else is exact.
- **Bare `m` is excluded as a unit.** Far too common as something else — size M,
  the M3 chip, model M240. The cost is asymmetric: a wrongly-extracted quantity
  *blocks a correct merge*, while a missed one only defers to the embeddings. The
  cheaper failure wins.

---

## Part 10 — The thing this project refuses to do

**Have this answer ready. It is the most likely place to be challenged, and being
first to it is worth more than any feature.**

The requirement was: search a phone, see it's ₹70,000 on Amazon and ₹65,000 on
Flipkart, with links to both.

**The Amazon and Flipkart prices cannot be obtained legitimately.** Amazon's terms
prohibit scraping; its Product Advertising API needs an approved Associates
account with qualifying sales. Flipkart's affiliate programme is closed to new
signups.

So a price attributed to Amazon here would be **invented** — a fabricated record
of a real company's pricing. Indefensible in a review, and actively harmful if
the app is ever deployed publicly.

What the project does instead is split links into two kinds and never conflate
them:

| Kind | Where it comes from | Price? |
|---|---|---|
| **Verified offer** | the source itself (eBay's own URL) | **Yes** — the exact offer shown |
| **Retailer search** | constructed: `amazon.in/s?k=…` | **No** |

Building a search URL is not scraping and breaks no terms. It lands the user on
the retailer's real results, where the real price is. The UI styles the two
differently and labels the second *"no price claimed."*

> **The engineering point worth making:** there is a test called
> `test_retailer_links_never_carry_a_price`. A rule stated only in a comment is
> one edit away from being crossed silently — so the boundary is enforced by the
> suite, not by good intentions.

---

## Part 11 — The recommender, and what it deliberately is not

"Customers who bought this also bought" is collaborative filtering, which needs an
interaction matrix — who bought what. **This project has no users.**

The usual shortcut is to invent random purchases. That produces random
recommendations that *look* exactly like a working recommender, and the difference
is invisible until someone reads the output carefully. **That is worse than
nothing.**

So relevance comes from three signals that need no user data:

| Signal | Weight | What it is |
|---|---|---|
| Complement | 1.00 | laptop → bag, dress → shoes, mascara → skincare |
| Substitute | 0.55 | same category, comparable price |
| Affinity | 0.35 | shared brand or tags |
| *Co-occurrence* | *1.20* | *reserved — real behaviour outranks every rule* |

Complements are defined per **category pair**, not per product, so a new product
inherits them for free. Same principle as the attribute extractor: nothing that
needs maintaining per item.

**Two rules were wrong on the real data and I fixed them:** `MacBook Pro →
Annibale Colombo Bed` and `Sofa → Lunch Box`. "A laptop goes with a desk and
chair" is sound in the abstract — but this catalogue's furniture is beds and
sofas. *A rule that is right in principle and wrong on the data is still wrong.*

> **Say it plainly:** "This is a content-and-rules recommender, not collaborative
> filtering. It cannot discover the non-obvious pairings CF finds from real
> behaviour. `score_from_cooccurrence` is the wired slot for a real dataset, and
> the blend already weights it above every rule."

---

## Part 12 — Measuring the resolver honestly on real data

The F1 = 1.000 result is on **15 hand-labelled listings**. On the live
194-product catalogue it is not 100%, and the number matters more than the claim.

Measured by counting clusters holding more offers than there are sellers —
if 4 sellers produce a 7-offer cluster, two products merged:

| | Value |
|---|---|
| Products checked | ~100 across 9 categories |
| Over-merged clusters | **8** (down from 10) |
| Cluster precision | **~92%** |

Two causes found and fixed:

- **Variant designators were being thrown away.** `iPhone 6` and `iPhone X` both
  reduced to `{iphone}` — the extractor drops tokens under three characters and
  bare digits, which are *exactly* the tokens that separate models in a line.
- **Word-named product lines had nothing to veto them.** Rolex Datejust, Cellini
  and Submariner share a brand and a string shape; TF-IDF character n-grams
  scored them near-identical. Stripping the brand before comparing distinctive
  words fixed most of it — the brand is the token those titles share, so leaving
  it in makes every product from one maker look related.

**A fix that made things worse, and how it was caught.** The first version of the
variant rule dropped gold-set recall from 1.000 to 0.900 — it split one real
product, because retailers write both "Nord CE 4" and "Nord CE4", which tokenise
to `{4}` and `{ce4}`. Comparing from the first digit onwards makes those agree
while keeping `s7`/`s10` and `6`/`16` apart.

> **The judgement call worth defending:** every further rule I tried broke the
> gold set. So I stopped and reported the number instead of chasing it. TF-IDF
> character n-grams fundamentally cannot tell "Beef Steak" from "Fish Steak" when
> the rest of the string matches — that is not a rule problem, it is a model
> problem. The principled fix is the transformer backend, which is a
> configuration change. Knowing when to stop adding rules is part of the work.

---

## The three lessons to leave a mentee with

1. **A silent bug is worse than a crash.** Every bug in Part 1–3 had zero error
   messages. The price history *looked healthy* — the row count was going up.
   Ask of any system: what could be broken right now while every indicator I'm
   watching says fine?

2. **Fixing a bug is ordinary; making that class of bug loud is engineering.**
   Every fix here came with a mechanism that surfaces the problem — `flat_series`
   on `/health`, FX-snapshot age, `diagnostics` in every response, a CI assertion.
   The next occurrence announces itself.

3. **Documentation is a claim; tests are evidence.** The README said responses
   were cached. They weren't. It said vector normalisation collapses the ranking.
   Sometimes it doesn't. Both claims were written honestly and both were wrong.
   The difference between a claim and a fact is whether something *executes* and
   checks it.

---

## Questions you will be asked

**"Isn't using simulated data cheating?"**
Not if you say so, and we say so in five places. It validates the forecasting
*pipeline*, not forecast *accuracy*. Every row is tagged `is_synthetic=1`,
`/health` reports `degraded` while all data is synthetic, and the eBay path is
already implemented and tested — the moment credentials exist, real data flows
through unchanged. The alternative was waiting 30 days doing nothing.

**"Why not just use a live exchange-rate API?"**
Reproducibility. If rates move between runs, the same query returns a different
winner and no result in the paper can be replayed. It's also one less network
dependency that can fail during a demo. Intraday FX precision is irrelevant when
the price spread between sellers is several percent. The snapshot's age is
reported on `/health`, so staleness is visible.

**"Why is `/health` returning `degraded`? Is something broken?"**
No — it's correctly reporting that all price data is synthetic and no live source
is configured. That's a data-quality warning, not a process failure. Note it
still returns HTTP 200: restarting the container wouldn't fix a missing API key,
so it shouldn't look like a crash to an orchestrator.

**"You changed one of your own results. Doesn't that weaken the paper?"**
The opposite. The original was a conclusion without a condition, verified on one
15-row hand-built dataset. We now report the mechanism (vector normalisation
preserves coefficients of variation) and the condition (it collapses at a ~24×
CV ratio, not at ~9×), and we show min-max is responsive in both. That's a
stronger claim, and it's reproducible with one command.

**"Why min-max instead of textbook TOPSIS?"**
Because min-max's responsiveness doesn't depend on how the criteria happen to be
distributed for whatever product the user searched for. We implement both — pass
`normalisation=vector` to reproduce the textbook behaviour.

**"What's still missing?"** Be direct:
- the labelled evaluation set is still only 15 listings / 7 clusters — F1 = 1.000
  on that is a smoke test, not a result. **This is the biggest remaining gap.**
- no recommender yet
- no forecasting model yet (but no longer blocked — the data exists)
- all price data is synthetic
- entity resolution is ~92% precise on the live catalogue, not 100% (Part 12)
- the recommender is rules-and-content, not collaborative filtering (Part 11)
- catalogue prices are reference prices from a demo API, not retailer quotes
- the legacy `dashboard.html` still reimplements TOPSIS in JavaScript; the live
  frontend at `web/app/` does not

---

## Demo running order

Roughly 6 minutes.

```bash
# 1. the tests pass                                       (~10s)
python -m pytest tests -q

# 2. the price history is real and NOT flat               (~30s)
python -m scripts.backfill_prices --days 180
#    -> point at "flat (unforecastable): 0"
#    -> point at the sample series it prints: the spread is tens of percent,
#       and the "last 8" line shows the price moving day to day

# 3. the pipeline, four stages                            (~10s)
python -m scripts.demo
#    -> 15 listings collapse into 7 products
#    -> three shopper profiles, three different winners

# 4. the ablation                                          (~5s)
python -m scripts.evaluate_matching

# 5. the corrected result                                  (~5s)
python -m scripts.analyse_normalisation
#    -> read the CONCLUSION block aloud

# 6. the live API
python -m scripts.serve
#    -> /health      : show status=degraded and explain WHY that's correct
#    -> /docs        : self-documenting API
#    -> /search twice with different weights -> different winner
```

Finish on step 6. Different weights, same query, different winner — that is the
project's argument, demonstrated live rather than asserted on a slide.
