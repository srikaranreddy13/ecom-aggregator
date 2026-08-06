# Demo Script

Every command, in order, with what to say while it runs and what to do when
something breaks.

**Total time:** ~12 minutes talking, ~3 minutes of commands.
**Companion docs:** [PROJECT_NOTES.md](PROJECT_NOTES.md) for the *why* behind
each fix, [README.md](README.md) for the reference.

---

## Before you start — do this 10 minutes early, not live

Run through the whole thing once. Never demo a command you have not run today.

```
cd C:\Users\srika\Downloads\project-v2\ecom-aggregator
```

Every command below assumes you are in this folder. **Almost every failure in
this project is being in the wrong directory.** Check with `dir` — you should
see `src`, `scripts`, `tests`, `web`.

### Set the fast backend (once per terminal window)

```
set EMBEDDER_BACKEND=tfidf
```

PowerShell instead of cmd:

```
$env:EMBEDDER_BACKEND="tfidf"
```

> **Why:** the default tries to load `sentence-transformers`, which imports
> PyTorch and can stall for 30 seconds with no output. Nothing looks worse than
> a frozen terminal in front of an audience. This forces the fast TF-IDF path.
> Every result in the README was produced on it.

### Pre-flight checklist

```
python -m pytest tests -q
python -m scripts.backfill_prices --days 180
```

Confirm two things before you go anywhere near the projector:

- the tests say **`215 passed`**
- the backfill says **`flat (unforecastable): 0`**

If either is wrong, fix it now. See Troubleshooting at the bottom.

### Open two terminals

| Terminal | Purpose |
|---|---|
| **A** | the API server — starts once and stays running |
| **B** | everything else — tests, scripts |

Trying to do both in one window means killing the server to run a script, and
you will lose the thread.

---

## Part 1 — The tests (45 seconds)

**Terminal B:**

```
python -m pytest tests -q
```

**Say while it runs:**

> "215 tests. It started at 11. Most of the new ones are named after a specific
> bug — the test name is documentation that can't go stale, because if the
> behaviour changes the test fails."

**Then show one name specifically:**

```
python -m pytest tests -q -k currency -v
```

**Say:**

> "That one — `test_usd_listing_no_longer_beats_inr_on_price` — is a real bug we
> found. Prices were bare numbers and the ranking compared them directly. eBay
> returns dollars, our other sources return rupees. So a $699 phone scored as
> cheaper than the same phone at ₹66,999, because 699 is less than 66,999.
>
> It was invisible because only the rupee source was switched on. The day we
> enabled eBay credentials, every ranking would silently have become wrong."

---

## Part 2 — The price history (60 seconds)

**Terminal B:**

```
python -m scripts.backfill_prices --days 180
```

**Point at the last block of output.** Say:

> "This is the most important bug we fixed, and it's the one I'd want you to
> take away.
>
> The daily price collector was pointed at a *static JSON file*. So every day it
> recorded the identical price. Thirty rows, two days, zero variance — a
> perfectly flat line. And the row count was going *up*, so every indicator said
> healthy.
>
> Forecasting a constant isn't a hard problem, it's an undefined one. We could
> have run that collector for six months, sat down to build the model, and found
> out on that day that we had six months of nothing."

**Then point at `flat (unforecastable): 0`:**

> "Fixing the bug was the easy half. This number is the other half — it counts
> series with more than one observation and zero price variance. It's on the
> health endpoint and it's asserted in CI. That class of bug can't hide silently
> again.
>
> Fixing a bug is ordinary. Making that class of bug *loud* is the engineering."

**Then point at the sample series at the bottom** — the `last 8:` line shows the
price moving day to day.

**If asked "is that real data?"** — answer before they finish asking:

> "No, it's simulated, and the system says so in five places. Every row is tagged
> `is_synthetic=1`, the sources are prefixed `sim_`, and the health endpoint
> reports `degraded` while all data is synthetic. It proves the forecasting
> *pipeline* works. It says nothing about whether a model predicts the real
> market. Those are different claims and we made it structurally hard to confuse
> them.
>
> The trick is that it's deterministic — every price comes from
> `hash(seed, item, date)`. So we can generate the history we *would* have
> collected since February, and forecasting is developable today instead of after
> a 30-day wait."

---

## Part 3 — Start the API (15 seconds)

**Terminal A:**

```
set EMBEDDER_BACKEND=tfidf
python -m scripts.serve
```

It prints four URLs. Leave this window running for the rest of the demo.

**Say:**

> "One process serves both the API and the frontend, so there's one thing to
> start."

**If the port is taken:** `python -m scripts.serve --port 9000` and use 9000
everywhere below.

---

## Part 4 — The frontend (5 minutes) — this is the main event

Open **http://127.0.0.1:8000** in a browser.

### 4a. Tab 1 — Search & Rank

**Say:**

> "Everything on this page came from the API at runtime. The browser does not
> rank anything — every score comes from the Python. That matters because the old
> dashboard re-implemented TOPSIS in JavaScript: two implementations of one
> algorithm that could disagree, and the JavaScript one was what people looked
> at. Now there's one."

**Point at the "This query" panel on the right.** The four pipeline stages are
listed with live numbers: *28 raw listings → 7 products (−21 duplicates)*.

> "Four sellers, 28 listings, and entity resolution collapsed them into 7 actual
> products. Twenty-one of those listings were duplicates of something else."

### 4b. THE MONEY SHOT — click the three presets in order

Click **💰 Budget hunter**, then **⚡ In a hurry**, then **🛡️ Trust first**.

**Watch the crown move.** Say:

> "Same query. Same products. Three different winners.
>
> That is the entire argument of this project: there is no single 'best' offer.
> Cheapest, fastest and most trusted are three different sellers, so the user's
> priorities have to decide. The system doesn't pick for you — it ranks against
> what you said you wanted."

**Then drag a slider slowly.** It re-ranks live (debounced, so it fires one
request at the end of the drag rather than forty during it).

**Then point at the green "Why X won" bars at the bottom of a product:**

> "And it explains itself. Those bars are per-criterion closeness to the ideal —
> so you can see the cheapest seller won on price and lost on delivery and
> rating. The explanation comes from the same TOPSIS call, not a second
> calculation."

### 4c. The normalisation toggle — the honest-correction moment

Click the **`?`** next to "Normalisation", read the table, close it. Then click
**Vector (textbook)**.

**Say:**

> "This is where we corrected one of our own published results.
>
> We originally reported that textbook TOPSIS collapses all three shopper
> profiles onto the same winner, and min-max keeps them responsive. On our
> 15-listing fixture that's exactly what happens.
>
> Then we tested it on a second dataset with a more realistic price spread — and
> it didn't hold. Vector normalisation gave three distinct winners too.
>
> The claim wasn't wrong, it was *under-specified*. We'd reported a conclusion
> when we should have reported a mechanism and a condition. Vector normalisation
> preserves each criterion's coefficient of variation, so it suppresses whichever
> criterion varies least. That only shows up as a collapse when the variation gap
> is large — 24× in the fixture, 8.7× in the realistic data.
>
> Min-max is responsive in both. *That's* the actual argument for it: its
> behaviour doesn't depend on how the criteria happen to be distributed for
> whatever the user searched."

> **Teaching point to add:** "We only found this because we tested the claim on a
> *second* dataset. A result verified on one hand-built 15-row fixture is a
> hypothesis. Our own fixture had been quietly flattering us."

Switch back to **Min-max (ours)** before moving on.

### 4d. Tab 2 — Entity Resolution

Click the tab, then **type `sony` in the filter box and click Resolve**. This is
the best single visual in the app.

**Say, pointing at the screen:**

> "Both of these are Sony headphones, both black, neither reports storage. So
> they land in the *same block* — you can see the block key at the top,
> `sony | none | black`.
>
> But they're two separate clusters. Look at the chips on the right:
> `wh1000xm4` and `wh1000xm5`. Different model numbers, so they can never merge,
> no matter how similar the text is.
>
> And the text *is* similar — 'Sony WH-1000XM4 Wireless Headphones Black' versus
> 'WH-1000XM5 Wireless Noise Cancelling Headphones, Black'. An embedding model
> scores those about 97% similar. Embeddings capture *topic*, not
> *specification*.
>
> That's the core design decision in the project: hard attributes are enforced as
> **constraints**, never learned. Two stages — rules first to make impossible
> merges impossible, embeddings second to handle reordered and paraphrased
> titles inside each block. Neither works alone."

**Then clear the filter and click Resolve** to show the whole catalogue grouped
into blocks.

**Mention the bug:**

> "Finding that model number was a bug we fixed. The original regex silently
> returned nothing when a code ended in a digit after letters — so `WH1000XM5`
> extracted as nothing, and XM4 and XM5 merged into one product. It's a
> regression test now."

### 4e. Tab 3 — Price History

Click through a few items in the sidebar.

**Say:**

> "180 days per item. Notice the shape — there's a depreciation trend downward,
> weekly wobble, and those sharp dips are sale events that persist for a few days
> rather than flickering on and off daily.
>
> That last detail matters: independent daily coin flips would give a sawtooth no
> real price ever shows, and there'd be nothing for a model to learn."

**Point at "spread of mean" and "since start".**

> "That's the difference between this and a flat line. This is a forecastable
> series."

### 4f. Tab 4 — System Health

**Say:**

> "Most health endpoints answer 'is the process running?' — which is nearly
> useless, because the process ran fine the whole time the price history was
> garbage.
>
> This one reports the failures this system can actually have. `flat_series` is
> zero. `real_observations` is zero, and it's warning me about that — it's telling
> me that any accuracy figure I compute right now measures the simulator, not the
> market.
>
> `status: degraded` is *correct*, not a malfunction. It's the system refusing to
> let me pretend simulated data is real. It still returns HTTP 200, because
> restarting the container wouldn't fix a missing API key — so it shouldn't look
> like a crash to an orchestrator."

Scroll down to the raw JSON.

> "And that's exactly what the API returned — nothing on this page is hand-typed."

### 4g. The API itself

Open **http://127.0.0.1:8000/docs** in a new tab.

> "Auto-generated from the endpoint definitions, so the documentation can't drift
> from the code. You can call any endpoint from here."

Try `/search` in the docs UI, or just hit a URL directly:

```
http://127.0.0.1:8000/search?q=iphone&price=0.9&delivery_days=0.05&seller_rating=0.05
http://127.0.0.1:8000/search?q=iphone&price=0.05&delivery_days=0.05&seller_rating=0.9
```

> "Different weights, same query, different winner — as raw JSON. The frontend is
> just a client."

---

## Part 5 — The evaluation tables (90 seconds)

**Terminal B** (the API keeps running in A):

```
python -m scripts.evaluate_matching
```

**Say:**

> "The ablation. Embeddings alone get F1 0.714 — they merge the 128GB and 256GB
> iPhones because the titles are nearly identical. Blocking alone gets 0.909 —
> rules can't handle reordered or paraphrased titles. Together: 1.000.
>
> And I'll say the caveat before you do: that's 15 listings and 7 clusters. F1 of
> 1.000 on a set that small is a smoke test, not a result. Getting to 500–1000
> hand-labelled pairs is the biggest remaining gap in the project."

```
python -m scripts.analyse_normalisation
```

**Read the CONCLUSION block at the bottom out loud.** It's written to be read
aloud.

---

## Part 6 — Close on version control (30 seconds)

```
git log --oneline
git status
```

> "Under version control, with CI configured. Every push runs all 215 tests on
> two Python versions, asserts the price history isn't flat, and boots the
> Docker container to check the health endpoint."

---

## The three lines to land

If they remember nothing else:

1. **"A silent bug is worse than a crash."** Every bug we fixed had zero error
   messages. The price history *looked healthy* — the row count was going up.
2. **"Fixing a bug is ordinary; making that class of bug loud is engineering."**
   Every fix shipped with something that surfaces the next occurrence.
3. **"Documentation is a claim; tests are evidence."** The README said responses
   were cached. They weren't. It said vector normalisation collapses the ranking.
   Sometimes it doesn't. Both were written honestly and both were wrong.

---

## Command cheat-sheet

```
set EMBEDDER_BACKEND=tfidf                        always do this first

python -m pytest tests -q                         215 tests
python -m pytest tests -q -k currency -v          one bug's tests

python -m scripts.backfill_prices --days 180      generate price history
python -m scripts.backfill_prices --purge-flat --dry-run   audit bad series

python -m scripts.serve                           API + frontend -> :8000
python -m scripts.serve --port 9000               if 8000 is taken

python -m scripts.demo                            terminal walkthrough
python -m scripts.evaluate_matching               ablation table
python -m scripts.analyse_normalisation           the corrected result
python -m scripts.collect_prices                  daily snapshot
python -m scripts.build_dashboard                 static snapshot page

git log --oneline
```

URLs once the server is up:

```
http://127.0.0.1:8000            the frontend
http://127.0.0.1:8000/docs       interactive API documentation
http://127.0.0.1:8000/health     diagnostics
```

---

## Troubleshooting — when it breaks in the room

| Symptom | Cause | Fix |
|---|---|---|
| `'export' is not recognized` | `export` is bash; you are in cmd.exe | `set EMBEDDER_BACKEND=tfidf` |
| `set` seems to do nothing in PowerShell | `set` is cmd.exe syntax | `$env:EMBEDDER_BACKEND="tfidf"` |
| `No module named 'scripts'` | wrong folder | `dir` — you must see `src`, `scripts`, `tests`. `cd` into the folder that has them |
| `No module named 'fastapi'` | API extras missing | `python -m pip install fastapi uvicorn pydantic httpx` |
| `No module named 'numpy'` | core deps missing | `python -m pip install -r requirements.txt` |
| Terminal hangs ~30s, no output | loading PyTorch | Ctrl-C, `set EMBEDDER_BACKEND=tfidf`, retry |
| Frontend shows "API unreachable" | server not running, or wrong port | check Terminal A; the status chip top-right tells you |
| Page loads but no products | no listings matched | clear the search box and press Enter |
| `flat (unforecastable)` is not 0 | leftover rows from the old collector | `python -m scripts.backfill_prices --purge-flat` |
| Price History tab is empty | no history generated | `python -m scripts.backfill_prices --days 180` |
| Port 8000 already in use | something else has it | `python -m scripts.serve --port 9000` |
| `git` not recognised | terminal predates the PATH change | restart VS Code, or `set PATH=%PATH%;%LOCALAPPDATA%\Programs\PortableGit\cmd` |
| Tests say 11 passed | old copy of the project | you are in the wrong folder |
| Browser shows a stale page | cached CSS/JS | Ctrl-Shift-R (hard reload) |

**The universal fallback.** If the frontend fails entirely, the terminal demo
proves the same points and depends on nothing but Python:

```
python -m scripts.demo
python -m scripts.evaluate_matching
python -m scripts.analyse_normalisation
```

Know this before you walk in. A demo with a fallback is a demo you can stay calm
during.

---

## Questions you will be asked

Fuller answers in [PROJECT_NOTES.md](PROJECT_NOTES.md).

**"Isn't simulated data cheating?"**
Not if you declare it, and it's declared in five places. It validates the
forecasting *pipeline*, not forecast *accuracy*. The eBay path is already
implemented and tested against a saved response fixture — the moment credentials
exist, real data flows through unchanged. The alternative was waiting 30 days
doing nothing.

**"Why is the status 'degraded'? Is it broken?"**
No. It's correctly reporting that all data is synthetic and no live source is
configured. A data-quality warning, not a process failure — which is why it still
returns HTTP 200.

**"You changed one of your own results — doesn't that weaken the paper?"**
The opposite. The original was a conclusion with no stated condition, verified on
one 15-row dataset. We now report the mechanism and the condition, and show
min-max is responsive in both. Stronger claim, reproducible in one command.

**"Why not use a live exchange-rate API?"**
Reproducibility. If rates move between runs, the same query returns a different
winner and no result can be replayed. It's also one less network dependency that
can fail during a demo. The snapshot's age is reported on `/health`, so staleness
is visible rather than silent.

**"What's still missing?"** Say it plainly — being first to your own limitations
is worth more than any feature:

- the labelled evaluation set is 15 listings / 7 clusters. **Biggest gap.**
- no recommender yet
- no forecasting model yet — but no longer blocked, the data exists
- all price observations are synthetic
- the repo has no remote, so no off-machine backup
