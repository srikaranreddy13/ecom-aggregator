# Review Notes — what is implemented, and how

Study document for the Stage-I review. Read top to bottom once; after that the
"Numbers to memorise" and "Questions you will be asked" sections are enough to
revise from.

Every claim here is something the code actually does. Where a limitation exists it
is stated, because volunteering a limitation is far stronger than being caught on
it.

---

## PART 0 — The 60-second answer

If the examiner says *"tell me about your project"*, say this:

> "Online shoppers compare the same product across many retailers by hand,
> because every site ranks by its own priorities rather than the buyer's. Our
> system takes one query, fetches real offers from many retailers at once, works
> out which listings are the *same physical product* despite completely different
> titles, and then ranks those offers against what *this particular shopper*
> values — cheapest, fastest, or most trusted.
>
> The hard part is not the ranking, it's deciding what counts as the same
> product. 'iPhone 15 128GB Blue' and 'iPhone 15 256GB Blue' are almost identical
> as text but are different products. So we combine rules with semantics: rules
> enforce hard specifications as constraints, and embeddings handle the fact that
> retailers word titles differently.
>
> The whole thing is one query in, ranked and explained offers out, and it works
> on phones, laptops, clothing, groceries and appliances without any
> category-specific code."

Then stop and let them ask. Do not recite the module list unprompted.

---

## PART 1 — The data flow, end to end

This is the single most useful thing to know. Walk this path and you can answer
almost anything.

```
  User types "iphone 15 128gb"
        │
   [1] SOURCE ADAPTER          → 40 raw listings from many retailers
        │
   [2] CURRENCY NORMALISATION  → every price in one currency
        │
   [3] RESPONSE CACHE          → repeat queries cost nothing
        │
   [4] ENTITY RESOLUTION       → 40 listings become 29 distinct products
        │                         Stage A: blocking
        │                         Stage B: semantic clustering
        │                         Stage C: specification veto
   [5] MCDM RANKING (TOPSIS)   → offers inside each product ranked
        │
   [6] THREE PROFILES          → budget / hurry / trust computed per product
        │
   [7] RECOMMENDER + LINKS     → related products, buy links
        │
   [8] REST API → WEB UI
```

**Real observed numbers for that exact query:** 40 listings in, 29 products out,
largest product cluster holds **10 competing offers**, price range
**₹40,900 – ₹71,190**.

---

## PART 2 — Module by module

For each: *what it does*, *how it works*, *why built that way*, *what to say*.

---

### [1] Source adapters — `src/adapters/`

**What.** Fetches offers. Every source implements one interface.

**How.** An abstract `SourceAdapter` class with a single method
`search(query, limit) -> list[Listing]`. `Listing` is a canonical dataclass:
source, item id, title, price, currency, delivery_days, seller_rating,
seller_name, url, image_url, brand.

Two live implementations:
- **Google Shopping via SerpApi** — real retailers, real prices. This is the
  main source.
- **Simulated market** — four seller archetypes over a local product list, used
  for offline demos and for tests.

**Why.** Nothing downstream knows or cares where a listing came from. The
matching, ranking and recommendation code never learns whether data came from an
API, a simulation or a cache. That is what let us swap the entire data source
without touching the ML code.

**What to say:**
> "One interface, so the algorithms are independent of the data source. We
> changed our live source completely during development and none of the matching
> or ranking code changed."

---

### [2] Currency normalisation — `src/money.py`

**What.** Converts every price into one currency before ranking.

**How.** A pinned FX snapshot in `data/fx_rates.json` (44 currencies, units per
1 USD). Conversion happens once, at the pipeline boundary. The original price and
currency are preserved in the listing so the UI can show "$699 (converted)".

**Why — this was a real bug, not a nicety.** `Listing.price` is a plain number and
TOPSIS compares those numbers directly. Our sources emit rupees; some emit
dollars. Without conversion the ranking compared `699 < 66999` and concluded a
$699 phone was **100× cheaper** than the same phone at ₹66,999. It was invisible
because only the rupee source was switched on — the bug was dormant purely
because the live source wasn't enabled yet.

**Three design decisions worth explaining:**

1. **Rates are a pinned file, not a live API call.** If rates moved between two
   runs, the same query would return a different winner and no result could be
   reproduced. Reproducibility beats freshness. It also removes a network
   dependency that could fail during a demo. The snapshot's age is reported so
   staleness is visible, not silent.
2. **Conversion is non-destructive.** Never throw away the input.
3. **An unconvertible currency is tagged, not dropped and not crashed.** A price
   we cannot convert is **not comparable** — if left untagged it can silently win
   a price ranking. Discovered exactly that: a Danish price in DKK, a currency
   missing from the table, was compared as if it were rupees and won outright.
   The table grew from 9 currencies to 44, and unconvertible prices are now
   counted and reported.

**What to say:**
> "The lesson was that a lookup table is a silent failure surface. The conversion
> code was correct; the table was incomplete, and the symptom was a wrong ranking
> that looked exactly like a correct one."

---

### [3] Response cache — `src/storage/db.py`

**What.** SQLite cache of source responses.

**How.** Key is `(version, adapter, limit, query, category)`. TTL configurable.
A cache hit returns rehydrated `Listing` objects.

**Why.** Our live source allows **250 searches per month**. Without caching, a
handful of page refreshes would exhaust the plan. With it, browsing, re-ranking
and dragging a weight slider all replay a cached response — **17 ms instead of
1121 ms** — and only a genuinely new query spends a credit. Verified: three
identical searches consumed one credit.

**A bug worth mentioning if asked about caching.** The key originally omitted the
category. Since a category chip sends an *empty query plus a category*, every
category collapsed onto one key and each one was served the first one's response
— every category showed laptops. **A cache key that omits an input which changes
the result is a correctness bug, not a performance one.**

---

### [4] Entity resolution — `src/matching/` ← **THE CORE CONTRIBUTION**

Spend most of your time here. This is what the project is actually about.

#### The problem

`"iPhone 15 128GB Blue"` and `"iPhone 15 256GB Blue"` are nearly identical as
text but are **different products**. Meanwhile
`"Apple iPhone 15 (128GB, Blue)"` and `"APPLE iPhone 15 128 GB Blue Smartphone"`
are worded quite differently and are the **same product**.

So neither approach works alone:
- **Embeddings alone** merge the 128GB and 256GB phones — embeddings capture
  *topic*, not *specification*.
- **Rules alone** cannot handle reordering, abbreviation and marketing noise
  ("Galaxy S24 5G 256 GB Black Samsung").

#### The solution: three stages

**Stage A — Blocking (coarse, exact, cheap).**
Partition listings on `(brand, colour)`. Different key ⇒ never compared.

> **Why deliberately coarse?** Block keys must match *exactly*. Every field added
> multiplies the chance that a listing which merely *omits* that field gets its
> own singleton block — where semantic clustering never runs and recall silently
> collapses. Blocking is a performance and safety mechanism, **not** the
> precision mechanism.

**Stage B — Semantic clustering.**
Inside each block, titles are normalised (marketing noise stripped), embedded,
and clustered by cosine similarity using agglomerative clustering with a
distance threshold.

Two embedding backends:
- **TF-IDF over character n-grams** — no model download, works offline, used for
  all reported results
- **sentence-transformers** — the transformer path, optional

The similarity threshold differs per backend (0.40 for TF-IDF, 0.72 for
transformer) because character-n-gram similarities are systematically lower.

**Stage C — Specification veto (must-not-link).**
Before clustering, any pair whose extracted specifications *disagree* is pushed
beyond the merge threshold, so no amount of textual similarity can join them.

Vetoed on: quantities, model number, generation, variant designator, clothing
size, pack count, and disjoint product names.

> **Crucially, the veto stays silent when either side is missing the attribute.**
> A title that omits RAM is not evidence of a different product. Only a stated
> *disagreement* counts.

#### The division of labour — this is the insight to articulate

| | Blocking | Veto |
|---|---|---|
| Granularity | coarse | fine |
| Matching | exact | tolerant of missing values |
| Purpose | partition (recall-safe) | precision |

**What to say:**
> "Precision and partitioning are different jobs. Partitioning must be coarse and
> exact so nothing gets exiled into a singleton block. Precision must be
> fine-grained and tolerant of missing data. Putting numeric precision into the
> block key looks stronger but destroys recall."

---

### [4b] Domain-agnostic attribute extraction — `src/matching/attributes.py`

**This is the most novel part. Know it well.**

**The problem with the obvious approach.** A category detector plus per-category
attribute rules ("phones have storage, appliances have wattage, clothes have
sizes") does not terminate. Every new domain needs another branch, and until
someone writes it, that domain resolves badly and *silently*.

**The universal rule instead:**

> **A quantity with a unit is a hard specification, in every domain.**

| Domain | Title fragment | Extracted |
|---|---|---|
| Electronics | iPhone 15 **256GB** | `gb: 256` |
| Appliance | Mixer **750W** | `w: 750` |
| Kitchen | Kettle **1.5 L** | `l: 1.5` |
| Grocery | Shampoo **500ml** | `l: 0.5` |
| Wearable | Watch **44mm** | `mm: 44` |
| TV | Bravia **55 inch** | `in: 55` |
| Clothing | Shirt **Size 42** | `size: 42` |
| Household | Bulbs **Pack of 4** | `pack: 4` |
| AC | **1.5 Ton 3 Star** | `ton: 1.5, star: 3` |

**One extractor. No taxonomy. No category branching anywhere.** A domain nobody
anticipated resolves correctly the first time it appears.

Units are normalised to a canonical dimension, so `1TB` equals `1024GB` and
`500ml` equals `0.5L`.

#### The comparison rule: maximum per unit

This is subtle and it fixed a real bug. A laptop title carries **two** GB figures:

```
"ThinkPad E14  16GB RAM  512GB SSD"
```

The old extractor scanned for the first plausible `\d+ ?GB` and returned **16** —
the RAM — as the storage size. Two laptops differing only in disk size shared a
block key on the wrong attribute.

Comparing **maxima per unit** gets all three cases right, and nothing has to know
which number means RAM:

| Comparison | Result | Correct? |
|---|---|---|
| `128` vs `256` | different | ✅ |
| `{16, 512}` vs `{16, 1024}` | different (512 ≠ 1024) | ✅ |
| `{128}` vs `{6, 128}` | **same** (128 = 128) | ✅ — omission isn't evidence |

Set *equality* fails the third case. Set *intersection* fails the second. The
maximum gets all three.

#### Two details that only realistic data revealed

1. **Screen sizes are marketing-rounded.** Apple sells the "13-inch" MacBook Air
   whose panel is **13.6"**, and retailers use both numbers for the same laptop.
   Exact matching split one product into two. So screen size and battery capacity
   compare with a small relative tolerance while everything else is exact.
2. **Bare `m` is excluded as a unit.** Far too common as something else — size M,
   the M3 chip, model M240. The cost is asymmetric: a wrongly-extracted quantity
   **blocks a correct merge**, while a missed one only defers to the embeddings.
   The cheaper failure wins.

#### Variant designators

Short model tokens are the most discriminative thing in electronics and were
being thrown away, because the extractor drops tokens under three characters and
bare digits. `iPhone 6` and `iPhone X` both reduced to `{iphone}`, shared a
token, escaped the veto and merged.

Now handled separately. And when comparing them, only the part from the first
digit onwards is used — because retailers write both "Nord CE 4" and "Nord CE4",
which tokenise to `{4}` and `{ce4}`. That normalisation makes those agree while
keeping `s7` vs `s10` and `6` vs `16` apart.

---

### [5] MCDM ranking — `src/ranking/mcdm.py`

**What.** Ranks the offers inside one product against a user weight vector.

**How — TOPSIS, in five steps:**

1. Build a decision matrix: alternatives (offers) × criteria (price, delivery,
   rating).
2. Normalise each column.
3. Apply user weights.
4. Find the ideal-best and ideal-worst value per criterion, respecting direction:
   - price → **cost** (lower is better)
   - delivery_days → **cost**
   - seller_rating → **benefit** (higher is better)
5. Score = `d_worst / (d_best + d_worst)`, in [0, 1], higher is better.

Weights are normalised, so only their *ratio* matters — `{8,1,1}` and
`{0.8,0.1,0.1}` rank identically.

**Explainability.** For the winning offer, the per-criterion closeness to the
ideal is returned and displayed — so the UI can show *why* this seller won.

**Missing values are imputed, not invented.** A seller with no rating is
*unknown*, not *bad* — zero would unfairly sink every new seller.

**A baseline is also implemented** — min-max weighted sum — so TOPSIS can be
compared against something in the evaluation.

---

### [6] Three shopper profiles

Computed **server-side for every product on every search**, not one at a time:

| Profile | price | delivery | rating |
|---|---|---|---|
| 💰 Budget hunter | 0.80 | 0.10 | 0.10 |
| ⚡ In a hurry | 0.20 | 0.65 | 0.15 |
| 🛡️ Trust first | 0.20 | 0.15 | 0.65 |

The response also carries `profiles_agree`, so the UI can say whether they
disagree.

**Why show all three at once?** The whole argument of the project is that they
disagree. Making the user click three times to discover that buries the finding.

**Real verified example** (iPhone 15 128GB):

| Profile | Winner | Price |
|---|---|---|
| Budget | icluster technologies | ₹40,900 |
| In a hurry | Vijay Sales | ₹59,900, 1-day |
| Trust | Vijay Sales | 4.6★ |

---

### [7] Recommender — `src/recommender.py`

**What.** "Customers also bought" for a product.

**How — three signals, blended:**

| Signal | Weight | What it is |
|---|---|---|
| Complement | 1.00 | cross-category: laptop → bag, dress → shoes |
| Substitute | 0.55 | same category, comparable price band |
| Affinity | 0.35 | shared brand or tags |
| *Co-occurrence* | *1.20* | *reserved — real behaviour outranks every rule* |

Complements are defined **per category pair, not per product**, so a new product
inherits them for free — the same principle as the attribute extractor.

Filters applied: never recommend the item itself; never recommend something
absurdly priced relative to it (a ₹90,000 dock for a ₹1,000 phone); cap any one
category so a watch query doesn't return six watches.

`min_score` is a **floor, not a suggestion** — three good suggestions beat ten
with seven duds. Padding is what makes a recommender feel broken.

**Say this plainly, before you are asked:**
> "This is a content-and-rules recommender, **not** collaborative filtering. CF
> needs an interaction matrix and we have no users. Inventing random purchases
> would produce random recommendations that look identical to a working
> recommender until someone reads the output carefully. The slot for a real
> behavioural dataset is wired and already weighted above every rule."

**Two rules were wrong on real data and were fixed:** `MacBook Pro → bed` and
`Sofa → lunch box`. "A laptop goes with a desk and chair" is sound in the
abstract but the catalogue's furniture was beds and sofas. *A rule that is right
in principle and wrong on the data is still wrong.*

---

### [8] Buy links — `src/links.py`

Two kinds, **never conflated**:

| Kind | Where from | Carries a price? |
|---|---|---|
| **Verified offer** | the source itself | **Yes** — the exact offer shown |
| **Retailer search** | constructed, e.g. `amazon.in/s?k=...` | **No** |

**What the project deliberately will not do:** display "Amazon: ₹70,000". Amazon
prohibits scraping and its API requires an approved Associates account with
qualifying sales; Flipkart's affiliate programme is closed to new signups. A
price attributed to either would therefore be **invented — a fabricated record of
a real company's pricing.** Building a search URL, by contrast, is just a URL: it
breaks no terms and still lands the user on the real page with the real price.

A test enforces that a search link can never carry a price, because a rule stated
only in a comment is one edit away from being crossed silently.

---

### [9] API and frontend

**REST API** (FastAPI, auto-generated docs at `/docs`):

| Endpoint | Purpose |
|---|---|
| `GET /search?q=…` | ranked offers, per-profile winners, images, links |
| `GET /categories` | browsable categories |
| `GET /recommendations?title=…` | related products |
| `GET /products` | resolved clusters + extracted attributes and block keys |
| `GET /health` | liveness **plus data-quality diagnostics** |

**Frontend:** vanilla HTML/CSS/JS. No framework, no build step, **no CDN** — so
it runs on a machine with no internet.

**The design rule to state:** *the browser does not rank anything.* Every score
comes from the Python. An earlier version reimplemented TOPSIS in JavaScript —
two implementations of one algorithm that can disagree, and the JavaScript one
was what people actually looked at. There is now one.

---

### [10] Testing — 226 automated tests

Worth explaining *how* they are written, not just how many:

- **Tests are named after the bug they prevent.**
  `test_usd_listing_no_longer_beats_inr_on_price` documents a real failure. A
  test name is documentation that cannot go stale — if the behaviour changes, the
  test fails.
- **Tests assert properties, not just values.** One checks that a price series is
  *autocorrelated* rather than white noise — a statistical property, not a
  hard-coded number.
- **Tests never touch external quota or the real database.** The suite forces the
  offline source, because a metered external API would make the suite both flaky
  and expensive.
- **Some tests protect the premise, not the code.** One fails if the test data is
  ever made degenerate — if one seller became best at everything, there would be
  a single objectively best offer and the project's premise would be unsupported
  by its own fixture.

**CI** runs the suite on two Python versions on every push.

---

## PART 3 — The two headline results, explained properly

### Result 1 — Entity resolution ablation

Pairwise precision/recall/F1 against a hand-labelled gold set of **15 listings /
7 clusters**:

| Configuration | Precision | Recall | F1 |
|---|---|---|---|
| Embeddings only | 0.556 | 1.000 | 0.714 |
| Blocking only | 0.556 | 1.000 | 0.714 |
| Blocking + embeddings | 0.556 | 1.000 | 0.714 |
| **Full hybrid (+ veto)** | **1.000** | **1.000** | **1.000** |

**The finding: the specification veto contributes the entire +0.286.** Blocking
and embeddings together add nothing over either alone on this set.

The evaluation also prints which pairs the veto stopped and how similar their
text was: **the XM4/XM5 pair sits at 0.625 cosine similarity** — it *would* have
merged on semantics alone.

**Volunteer the caveat before they raise it:**
> "F1 of 1.000 on 15 listings is a smoke test, not a result. Getting to 500–1000
> hand-labelled pairs is Stage-II's first task, and it's the single biggest gap in
> the project."

On the broader 194-product offline catalogue, cluster precision measured
**≈92%** (8 over-merged clusters out of ~100 products).

### Result 2 — Normalisation governs MCDM responsiveness

Textbook TOPSIS uses **vector** normalisation, which *preserves* each criterion's
coefficient of variation. Min-max *equalises* the spans.

Consequence: vector normalisation **suppresses whichever criterion happens to
vary least, no matter how much weight the user puts on it.** If price varies a few
percent between sellers while delivery varies several-fold, a weight of 0.8 on
price loses to 0.1 on delivery — the user's slider stops working.

But that only becomes a *visible collapse* when the variation gap is large enough:

| Dataset | CV(price) | CV(delivery) | Ratio | Vector normalisation |
|---|---|---|---|---|
| Fixture | 0.016 | 0.374 | **24×** | **collapses** to 1 winner |
| Wider spread | 0.054 | 0.474 | **8.7×** | stays responsive |

**Min-max is responsive in both — and that is the actual argument for it:** its
behaviour does not depend on how the criteria happen to be distributed for
whatever the user searched.

**This is a corrected result, and saying so is a strength:**
> "We originally reported that vector normalisation always collapses the ranking.
> Testing on a second dataset showed that's conditional, not universal. The claim
> wasn't wrong, it was *under-specified* — we'd reported a conclusion when we
> should have reported a mechanism and a condition. We only found it because we
> tested the claim on a second dataset. A result verified on one hand-built
> 15-row fixture is a hypothesis."

---

## PART 4 — Numbers to memorise

| Quantity | Value |
|---|---|
| Automated tests | **226** |
| Listings → products (one real query) | **40 → 29** |
| Largest product cluster | **10 competing offers** |
| Observed price range, iPhone 15 128GB | **₹40,900 – ₹71,190** |
| Hybrid entity-resolution F1 (gold set) | **1.000** |
| Ablation without the veto | **0.714** |
| Veto's contribution | **+0.286 F1** |
| Cluster precision, 194-product catalogue | **≈92%** |
| CV ratio where vector normalisation collapses | **24×** (not at 8.7×) |
| Cached response vs live | **17 ms vs 1121 ms** |
| Currencies in the FX table | **44** |
| Live search quota | **250 / month** |

---

## PART 5 — Questions you will be asked

**"Is the data real?"**
Yes. Live Google Shopping data: real Indian retailers — Vijay Sales, Croma,
R.R.Mobiles — with real rupee prices, real star ratings and real review counts.
The free tier allows 250 searches a month, which is why response caching is
essential rather than an optimisation.

**"Why not use Amazon's or Flipkart's API directly?"**
Amazon's terms prohibit scraping and its Product Advertising API requires an
approved Associates account with qualifying sales. Flipkart's affiliate programme
is closed to new signups. Google Shopping already aggregates Amazon, Flipkart,
Croma and hundreds of others, so one legitimate source reaches more retailers
than an Amazon-only integration would.

**"F1 = 1.000 — really?"**
On 15 hand-labelled listings, which is a smoke test rather than a result. Scaling
the labelled set is Stage-II's first task. On the broader 194-product catalogue,
cluster precision is ≈92%.

**"Why is your blocking so weak — only brand and colour?"**
Deliberate. Block keys must match exactly, so every field added risks exiling any
listing that omits it into a singleton block, where semantic clustering never
runs and recall collapses. All numeric precision lives in the veto, which stays
silent when a spec is missing.

**"Is the recommender collaborative filtering?"**
No — it's content-and-rules, because there is no user interaction data. Faking
purchases would produce random recommendations indistinguishable from a working
system. The slot for a real dataset is wired and weighted above every rule.

**"Why min-max instead of textbook TOPSIS?"**
Because min-max's responsiveness doesn't depend on how criteria happen to be
distributed for a given product. Both are implemented — pass
`normalisation=vector` to reproduce the textbook behaviour.

**"How does it handle a product category you didn't anticipate?"**
There are no categories in the code. The rule is that a quantity with a unit is a
hard specification, which holds in every domain — so a new category resolves
correctly the first time it appears. Demonstrated on electronics, appliances,
clothing, groceries, footwear and furniture with no domain-specific code.

**"What's the hardest problem you solved?"**
Deciding what counts as the same product. Two listings can be 97% similar as text
and be different products, or worded completely differently and be the same one.
Neither rules nor embeddings work alone.

**"What would you do differently?"**
Test claims on a second dataset much earlier. Two of our results — the
normalisation finding and the storage-size extractor — were wrong or
under-specified in ways only a second dataset exposed.

---

## PART 6 — What is NOT built (say it first, do not be caught)

1. **The labelled evaluation set is 15 listings / 7 clusters.** Biggest gap.
2. **Price forecasting is not implemented.** The storage schema and collection
   path exist; no model is trained.
3. **The recommender is not collaborative filtering.**
4. **Entity resolution is ≈92% precise on the broader catalogue, not 100%.** The
   residual cases are beyond string heuristics — `iPhone X` and `Realme XT` have
   letter-only model names with no digit to compare, and `Beef Steak` vs
   `Fish Steak` differ by one word while sharing the noun. Every additional rule
   tried broke the gold set, so the number is reported rather than chased. The
   principled fix is the transformer embedding backend, which is a configuration
   change rather than new code.
5. **No user accounts, no checkout.** Out of scope by design.

---

## PART 7 — The three lessons (good closing answer)

If asked *"what did you learn?"*:

1. **A silent bug is worse than a crash.** Several bugs we fixed produced no error
   at all — a price history whose row count grew while holding zero variance, a
   currency comparison that was simply wrong. Ask of any system: *what could be
   broken right now while every indicator I'm watching says fine?*

2. **Fixing a bug is ordinary; making that class of bug loud is engineering.**
   Every fix shipped with a mechanism that surfaces the next occurrence — a
   zero-variance counter, an FX-snapshot age, an incomparable-price count, a CI
   assertion.

3. **Documentation is a claim; tests are evidence.** Our own README stated that
   responses were cached when nothing called the cache, and stated a
   normalisation result more generally than the data supported. Both were written
   honestly and both were wrong. The difference between a claim and a fact is
   whether something executes and checks it.
