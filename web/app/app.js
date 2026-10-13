/*
  Live frontend logic.

  DESIGN RULE, and the reason this file exists at all: the browser does NOT rank
  anything. Every score, every winner and every "why" figure comes from
  GET /search, i.e. from src/ranking/mcdm.py. The previous dashboard
  re-implemented TOPSIS in JavaScript, which meant two implementations of one
  algorithm that could silently disagree -- and the JS one was the one people
  actually looked at. This file only renders what the API returns.

  No framework, no build step, no CDN. The project has to run offline.
*/

'use strict';

// ---------------------------------------------------------------- helpers

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Escape text before it goes anywhere near innerHTML. Titles come from a
 *  remote marketplace API, so they are untrusted input. */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

const nf0 = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const nf2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

let CURRENCY = 'INR';
const SYMBOL = { INR: '₹', USD: '$', EUR: '€', GBP: '£' };
const money = n => (SYMBOL[CURRENCY] || CURRENCY + ' ') + nf0.format(Math.round(n));

/** Fetch JSON, turning a non-2xx into a useful Error rather than a silent
 *  `undefined` three functions later. */
async function api(path) {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  const text = await res.text();
  let body;
  try { body = JSON.parse(text); }
  catch { throw new Error(`${path} returned non-JSON (HTTP ${res.status})`); }
  if (!res.ok) {
    const detail = body?.detail ?? body?.error ?? `HTTP ${res.status}`;
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return body;
}

function showError(el, err, hint) {
  el.innerHTML = `<div class="errorbox">
    <strong>Request failed.</strong> ${esc(err.message)}
    ${hint ? `<br><br>${hint}` : ''}
  </div>`;
}

function loading(el, label = 'running the pipeline…') {
  el.innerHTML = `<div class="loading"><span class="spinner"></span>${esc(label)}</div>`;
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

// ---------------------------------------------------------------- tabs

$$('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    $$('.tab').forEach(t => t.classList.toggle('active', t === tab));
    const view = tab.dataset.view;
    $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${view}`));
    if (view === 'resolve' && !state.resolveLoaded) runResolve();
  });
});

// ---------------------------------------------------------------- state

const state = {
  normalisation: 'minmax',
  category: null,       // null = all categories
  resolveLoaded: false,
  pricesLoaded: false,
  selectedItem: null,
  lastWinners: {},      // cluster_id -> source, used to flag a changed winner
};

// ============================================================ SEARCH & RANK

const weightEls = {
  price:         $('#w-price'),
  delivery_days: $('#w-delivery'),
  seller_rating: $('#w-rating'),
};
const outEls = { price: $('#out-price'), delivery_days: $('#out-delivery'), seller_rating: $('#out-rating') };

function currentWeights() {
  return {
    price:         parseFloat(weightEls.price.value),
    delivery_days: parseFloat(weightEls.delivery_days.value),
    seller_rating: parseFloat(weightEls.seller_rating.value),
  };
}

function syncWeightLabels() {
  const w = currentWeights();
  for (const k in outEls) outEls[k].textContent = w[k].toFixed(2);

  // Highlight a preset only when the sliders still match it exactly.
  const sig = [w.price, w.delivery_days, w.seller_rating].map(v => v.toFixed(2)).join(',');
  $$('.preset').forEach(b => {
    const bs = b.dataset.w.split(',').map(v => parseFloat(v).toFixed(2)).join(',');
    b.classList.toggle('active', bs === sig);
  });
}

// Re-rank as the slider moves. Debounced so dragging fires one request at the
// end of the gesture rather than forty during it.
const liveSearch = debounce(() => runSearch({ keepScroll: true }), 220);

Object.values(weightEls).forEach(el => {
  el.addEventListener('input', () => { syncWeightLabels(); liveSearch(); });
});

$$('.preset').forEach(btn => {
  btn.addEventListener('click', () => {
    const [p, d, r] = btn.dataset.w.split(',').map(Number);
    weightEls.price.value = p;
    weightEls.delivery_days.value = d;
    weightEls.seller_rating.value = r;
    syncWeightLabels();
    runSearch();
  });
});

$$('.seg').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.seg').forEach(b => b.classList.toggle('active', b === btn));
    state.normalisation = btn.dataset.norm;
    const note = $('#normNote');
    if (state.normalisation === 'vector') {
      note.textContent = 'Textbook TOPSIS. It preserves each criterion’s ' +
        'coefficient of variation, so whichever criterion varies least gets ' +
        'suppressed no matter how much weight you give it.';
      note.classList.add('warn');
    } else {
      note.textContent = 'Min-max rescales every criterion to the same span, ' +
        'so your weights control the outcome.';
      note.classList.remove('warn');
    }
    runSearch();
  });
});

$('#searchBtn').addEventListener('click', () => runSearch());
$('#q').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });

async function runSearch({ keepScroll = false } = {}) {
  const box = $('#results');
  const scrollY = window.scrollY;
  const w = currentWeights();

  if (w.price + w.delivery_days + w.seller_rating <= 0) {
    box.innerHTML = `<div class="errorbox"><strong>All weights are zero.</strong>
      With no criteria there is nothing to rank on. Raise at least one slider.</div>`;
    return;
  }

  if (!keepScroll) loading(box);
  const params = new URLSearchParams({
    q: $('#q').value.trim(),
    price: w.price, delivery_days: w.delivery_days, seller_rating: w.seller_rating,
    normalisation: state.normalisation,
  });
  if (state.category) params.set('category', state.category);

  try {
    const data = await api('/search?' + params);
    CURRENCY = data.results[0]?.currency || CURRENCY;
    renderStats(data);
    renderResults(data);
    $('#footerInfo').textContent =
      `${data.count} products · ${data.diagnostics.listings} listings · ` +
      `ranked server-side in ${data.took_ms} ms`;
    if (keepScroll) window.scrollTo({ top: scrollY });
  } catch (err) {
    showError(box, err,
      'Is the API running? Start it with <code>python -m scripts.serve</code>.');
  }
}

function renderStats(data) {
  const d = data.diagnostics;
  const perSource = d.per_source || {};
  const nSources = Object.keys(perSource).length;

  $('#statGrid').innerHTML = `
    ${stat(d.listings, 'raw listings')}
    ${stat(data.count, 'products')}
    ${stat(nSources, 'sources')}
    ${stat(data.took_ms + 'ms', 'server time')}
  `;
  animateStatTiles($('#statGrid'));

  const dedup = d.listings - data.count;
  const failures = Object.keys(d.failures || {}).length;

  $('#pipelineViz').innerHTML = `
    ${pstage(1, 'Ingestion — parallel fetch',
      Object.entries(perSource).map(([k, v]) => `${k}:${v}`).join('  ') || 'no sources')}
    ${pstage(2, 'Entity resolution — blocking + clustering',
      `${d.listings} → ${data.count}  (−${dedup} duplicates)`)}
    ${pstage(3, `MCDM ranking — TOPSIS (${data.normalisation})`,
      Object.entries(data.weights).map(([k, v]) => v.toFixed(2)).join(' / '))}
    ${pstage(4, 'Currency normalised to', d.base_currency)}
    ${failures ? pstage('!', 'Sources that failed', Object.keys(d.failures).join(', ')) : ''}
    ${(d.cache_hits || []).length ? pstage('⚡', 'Served from cache', d.cache_hits.join(', ')) : ''}
  `;
}

const stat = (n, k) => `<div class="stat"><div class="n">${esc(n)}</div><div class="k">${esc(k)}</div></div>`;

/** Counts a `.stat .n` tile up from 0 to its rendered value. Scoped to PURE
 *  integers only (no currency symbol, no "ms"/"d"/"★" suffix) -- those already
 *  carry their own visual weight, and tweening "₹59,900" digit-by-digit would
 *  flicker through nonsense intermediate amounts rather than read as motion. */
function animateStatTiles(container) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  $$('.stat .n', container).forEach(el => {
    const target = el.textContent.trim();
    if (!/^\d+$/.test(target)) return;
    const end = parseInt(target, 10);
    if (end === 0) return;
    const start = performance.now();
    const dur = Math.min(600, 150 + end * 4);
    (function tick(now) {
      const p = Math.min(1, (now - start) / dur);
      const eased = 1 - (1 - p) * (1 - p);
      el.textContent = Math.round(end * eased).toString();
      if (p < 1) requestAnimationFrame(tick);
      else el.textContent = target;
    })(start);
  });
}
const pstage = (num, lbl, val) =>
  `<div class="pstage"><span class="num">${esc(num)}</span>
     <span class="lbl">${esc(lbl)}</span><span class="val">${esc(val)}</span></div>`;

/** Profile metadata: label, icon and the weight vector the server used. */
const PROFILE_META = {
  budget: { icon: '💰', label: 'Budget hunter', hint: 'cheapest' },
  hurry:  { icon: '⚡', label: 'In a hurry',    hint: 'fastest' },
  trust:  { icon: '🛡️', label: 'Trust first',   hint: 'best rated' },
};

function profileStrip(p) {
  const winners = p.profiles || {};
  const distinct = new Set(Object.values(winners).map(w => w.source)).size;
  return `
    <div class="profiles">
      <div class="profiles-head">
        Best offer for each kind of shopper
        <span class="${distinct > 1 ? 'ok-pill' : 'warn-pill'}">
          ${distinct > 1 ? `${distinct} different winners` : 'all three agree'}
        </span>
      </div>
      <div class="profile-grid">
        ${Object.entries(winners).map(([key, w]) => {
          const m = PROFILE_META[key] || { icon: '•', label: key, hint: '' };
          return `<div class="profile-card">
            <div class="pc-head"><span class="pc-icon">${m.icon}</span>
              <span class="pc-label">${esc(m.label)}</span></div>
            <div class="pc-price">${money(w.price)}</div>
            <div class="pc-seller">${esc(w.seller_name || w.source)}</div>
            <div class="pc-facts">
              <span>${w.delivery_days ?? '—'}d</span>
              <span>${w.seller_rating ?? '—'}★</span>
            </div>
          </div>`;
        }).join('')}
      </div>
    </div>`;
}

function buyLinks(p) {
  const links = p.links || {};
  const verified = links.verified || [];
  const search = links.search || [];
  if (!verified.length && !search.length) return '';

  return `<div class="buy">
    ${verified.length ? `
      <div class="buy-row">
        <span class="buy-label verified">✓ Verified offers</span>
        ${verified.slice(0, 4).map(b => `
          <a class="buybtn verified" href="${esc(b.url)}" target="_blank" rel="noopener">
            ${esc(b.label)} · ${money(b.price)}</a>`).join('')}
      </div>` : ''}
    <div class="buy-row">
      <span class="buy-label">Find it on</span>
      ${search.map(b => `
        <a class="buybtn" href="${esc(b.url)}" target="_blank" rel="noopener">
          ${esc(b.label)} ↗</a>`).join('')}
      ${!verified.length ? `<span class="buy-note" title="${esc(links.note || '')}">
        no price claimed — opens the retailer's own results</span>` : ''}
    </div>
  </div>`;
}

function renderResults(data) {
  const box = $('#results');
  if (!data.results.length) {
    box.innerHTML = `<div class="empty">
      No products matched <strong>${esc(data.query || '(everything)')}</strong>${
        data.category ? ` in <strong>${esc(data.category)}</strong>` : ''}.
      Try <em>shirt</em>, <em>laptop</em>, <em>sofa</em>, <em>watch</em>, or pick a category above.
    </div>`;
    return;
  }

  box.innerHTML = data.results.map(p => {
    const spread = p.price_range[1] - p.price_range[0];
    const pct = p.price_range[1] ? (spread / p.price_range[1] * 100) : 0;
    const changed = state.lastWinners[p.cluster_id] &&
                    state.lastWinners[p.cluster_id] !== p.best_offer.source;
    state.lastWinners[p.cluster_id] = p.best_offer.source;

    const maxScore = Math.max(...p.all_offers.map(o => o.score)) || 1;

    return `<article class="product" data-title="${esc(p.product)}">
      <div class="product-head">
        ${p.image_url ? `<img class="thumb" src="${esc(p.image_url)}"
             alt="${esc(p.product)}" loading="lazy"
             onerror="this.classList.add('broken')">`
          : `<div class="thumb broken"></div>`}
        <div class="ph-main">
          <div class="product-title">${esc(p.product)}</div>
          <div class="product-meta">
            <span class="badge">${p.num_offers} offers</span>
            ${p.category ? `<span class="badge cat">${esc(p.category)}</span>` : ''}
            ${p.brand ? `<span class="badge">${esc(p.brand)}</span>` : ''}
            ${p.specs && p.specs !== 'no specs detected'
              ? `<span class="badge spec">${esc(p.specs)}</span>` : ''}
            ${changed ? '<span class="badge win">winner changed</span>' : ''}
          </div>
        </div>
        <div class="savings">
          <div class="amt">${money(spread)}</div>
          <div class="lbl">spread · ${pct.toFixed(1)}%</div>
        </div>
      </div>

      ${profileStrip(p)}
      ${buyLinks(p)}

      <div class="recwrap">
        <button class="recbtn" data-title="${esc(p.product)}">
          Customers also bought →</button>
        <div class="recs"></div>
      </div>

      <table class="offers">
        <thead><tr>
          <th>#</th><th>Seller</th><th class="num">Price</th>
          <th class="num">Delivery</th><th class="num">Rating</th><th>TOPSIS score</th>
        </tr></thead>
        <tbody>
          ${p.all_offers.map(o => `
            <tr class="${o.rank === 1 ? 'winner' : ''}">
              <td class="rank ${o.rank === 1 ? 'first' : ''}">${o.rank}${o.rank === 1 ? '<span class="crown">👑</span>' : ''}</td>
              <td>${esc(o.source)}</td>
              <td class="num money">${money(o.price)}
                ${o.original_currency && o.original_currency !== CURRENCY
                  ? `<div class="orig">${esc(o.original_currency)} ${nf2.format(o.original_price)}</div>` : ''}
              </td>
              <td class="num">${o.delivery_days ?? '—'}${o.delivery_days ? ' d' : ''}</td>
              <td class="num">${o.seller_rating ?? '—'}${o.seller_rating ? '★' : ''}</td>
              <td>
                <div class="scorebar">
                  <div class="scoretrack"><div class="scorefill" style="width:${(o.score / maxScore * 100).toFixed(1)}%"></div></div>
                  <span class="scorenum">${o.score.toFixed(3)}</span>
                </div>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>

      <div class="why">
        <div class="why-head">Why ${esc(p.best_offer.source)} won <em>your</em> weights — closeness to the ideal</div>
        <div class="why-bars">
          ${whyRow('Low price',     p.best_offer.why.price,         'price')}
          ${whyRow('Fast delivery', p.best_offer.why.delivery_days, 'delivery')}
          ${whyRow('Seller rating', p.best_offer.why.seller_rating, 'rating')}
        </div>
      </div>
    </article>`;
  }).join('');

  wireRecommendationButtons(box);
}

/** Lazy-load recommendations: one request per product the user actually opens,
 *  rather than N requests up front for panels most people never expand. */
function wireRecommendationButtons(root) {
  $$('.recbtn', root).forEach(btn => {
    btn.addEventListener('click', async () => {
      const panel = btn.parentElement.querySelector('.recs');
      if (panel.dataset.loaded === '1') {
        panel.classList.toggle('open');
        return;
      }
      btn.disabled = true;
      panel.innerHTML = `<div class="loading" style="padding:16px">
        <span class="spinner"></span>finding related products…</div>`;
      panel.classList.add('open');
      try {
        // `q` lets a search-driven source recommend from its own results. The
        // search is already cached, so this costs no API quota.
        const d = await api('/recommendations?' + new URLSearchParams({
          title: btn.dataset.title, k: 6,
          q: $('#q').value.trim() || state.category || '',
        }));
        panel.dataset.loaded = '1';
        panel.innerHTML = d.count ? `
          <div class="rec-grid">
            ${d.recommendations.map(r => `
              <a class="rec" href="${esc((r.search_url || '#'))}"
                 title="${esc(r.reason)}">
                ${r.image_url ? `<img src="${esc(r.image_url)}" alt=""
                     loading="lazy" onerror="this.style.display='none'">` : ''}
                <div class="rec-body">
                  <div class="rec-title">${esc(r.title)}</div>
                  <div class="rec-meta">${esc(r.category || '')}</div>
                  <div class="rec-why">${esc(r.reason)}</div>
                </div>
              </a>`).join('')}
          </div>
          <div class="rec-basis">${esc(d.basis)}</div>`
          : `<div class="empty" style="padding:18px">
               No related products found for this item.</div>`;
      } catch (err) {
        showError(panel, err);
      } finally {
        btn.disabled = false;
      }
    });
  });
}

// ---------------------------------------------------------------- categories

async function loadCategories() {
  const bar = $('#catChips');
  try {
    const d = await api('/categories');
    bar.innerHTML = `<button class="catchip active" data-cat="">All</button>` +
      d.categories.map(c => `
        <button class="catchip" data-cat="${esc(c.name)}"
          title="${c.count} products">${esc(c.label)}</button>`).join('');

    $$('.catchip', bar).forEach(chip => {
      chip.addEventListener('click', () => {
        $$('.catchip', bar).forEach(x => x.classList.toggle('active', x === chip));
        state.category = chip.dataset.cat || null;
        runSearch();
      });
    });
    $('#footerInfo').textContent =
      `${d.total_products} products · ${d.categories.length} categories · ` +
      `catalogue: ${d.source}${d.is_live ? ' (live)' : ' (offline fallback)'}`;
  } catch (err) {
    bar.innerHTML = `<span class="hint">categories unavailable</span>`;
  }
}

function whyRow(label, value, cls) {
  if (value === undefined || value === null) {
    return `<div class="why-row"><span class="k">${esc(label)}</span>
      <span class="hint">weight is zero — criterion excluded</span><span class="v">—</span></div>`;
  }
  return `<div class="why-row">
    <span class="k">${esc(label)}</span>
    <div class="why-track"><div class="why-fill ${cls}" style="width:${(value * 100).toFixed(1)}%"></div></div>
    <span class="v">${value.toFixed(2)}</span>
  </div>`;
}

// ---------------------------------------------------------------- norm modal

$('#normInfo').addEventListener('click', () => openModal(`
  <h3>Why the normalisation setting matters</h3>
  <p>TOPSIS has to put criteria measured in different units — rupees, days,
  stars — onto a comparable scale before weights mean anything. How you do that
  changes the answer.</p>
  <p><strong>Vector (textbook)</strong> divides each column by its magnitude.
  That <em>preserves</em> how much each criterion varies relative to its own
  average. So a criterion that barely varies between sellers stays compressed —
  and your weight on it cannot overcome that.</p>
  <p><strong>Min-max (ours)</strong> rescales every criterion to the same 0–1
  span, so weights control influence the way a user expects.</p>
  <p>We originally reported that vector normalisation always collapses every
  shopper profile onto one winner. Testing on a second dataset showed that is
  <em>conditional</em>, not universal — it depends on the ratio of coefficients
  of variation:</p>
  <table>
    <tr><th>Dataset</th><th>CV price</th><th>CV delivery</th><th>Ratio</th><th>Vector</th></tr>
    <tr><td>Mock fixture</td><td class="mono">0.016</td><td class="mono">0.374</td>
        <td class="mono">24×</td><td>collapses</td></tr>
    <tr><td>Synthetic market</td><td class="mono">0.054</td><td class="mono">0.474</td>
        <td class="mono">8.7×</td><td>responsive</td></tr>
  </table>
  <p>Min-max is responsive in <strong>both</strong>. That is the real argument for
  it: its behaviour does not depend on how the criteria happen to be distributed
  for whatever product was searched. Reproduce this with
  <code>python -m scripts.analyse_normalisation</code>.</p>
`));

function openModal(html) {
  $('#modalBody').innerHTML = html;
  $('#modal').hidden = false;
}
$('#modalClose').addEventListener('click', () => { $('#modal').hidden = true; });
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') $('#modal').hidden = true; });
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('#modal').hidden = true; });

// ============================================================ RESOLUTION

$('#resolveBtn').addEventListener('click', () => runResolve());
$('#rq').addEventListener('keydown', e => { if (e.key === 'Enter') runResolve(); });

async function runResolve() {
  const box = $('#blocks');
  loading(box, 'extracting attributes and clustering…');
  try {
    const data = await api('/products?' + new URLSearchParams({ q: $('#rq').value.trim() }));
    state.resolveLoaded = true;
    renderResolve(data);
  } catch (err) {
    showError(box, err, 'Is the API running? <code>python -m scripts.serve</code>');
  }
}

function renderResolve(data) {
  const r = data.resolver;
  const merged = data.products.filter(p => p.num_offers > 1).length;

  $('#resolveSummary').innerHTML = `
    ${stat(data.listings, 'raw listings')}
    ${stat(data.clusters, 'distinct products')}
    ${stat(merged, 'multi-offer clusters')}
    ${stat(r.backend, 'embedding backend')}
    ${stat(r.similarity_threshold, 'similarity threshold')}
  `;
  animateStatTiles($('#resolveSummary'));

  // Group clusters by the block key their listings share. Blocking is Stage 1,
  // so showing the blocks first makes the two stages visibly distinct.
  const blocks = new Map();
  for (const p of data.products) {
    const key = p.offers[0]?.block_key ?? '?';
    if (!blocks.has(key)) blocks.set(key, { attrs: p.offers[0]?.attributes, clusters: [] });
    blocks.get(key).clusters.push(p);
  }

  const box = $('#blocks');
  if (!blocks.size) { box.innerHTML = `<div class="empty">No listings matched.</div>`; return; }

  box.innerHTML = [...blocks.entries()].map(([key, blk]) => {
    const a = blk.attrs || {};
    const split = blk.clusters.length > 1;
    return `<div class="block">
      <div class="block-head">
        <span class="bk">block ${esc(key)}</span>
        ${attrChip('brand', a.brand)}
        ${attrChip('storage', a.storage_gb ? a.storage_gb + 'GB' : null)}
        ${attrChip('colour', a.colour)}
        <span class="hint" style="margin-left:auto">
          ${blk.clusters.length} cluster${blk.clusters.length > 1 ? 's' : ''}
          ${split ? '· split by semantics or a must-not-link constraint' : ''}
        </span>
      </div>
      ${blk.clusters.map(p => `
        <div class="cluster">
          <div class="cluster-head">
            <span class="cid">${esc(p.cluster_id)}</span>
            ${p.num_offers > 1
              ? `<span class="merged">✓ merged ${p.num_offers} listings</span>`
              : `<span class="singleton">single listing</span>`}
            <span class="hint">${esc(p.canonical_title)}</span>
          </div>
          ${p.offers.map(o => `
            <div class="listing">
              <span class="src">${esc(o.source)}</span>
              <span class="ttl" title="${esc(o.title)}">${esc(o.title)}</span>
              <span class="attrs">
                ${o.attributes.model_number ? attrChip('model', o.attributes.model_number) : ''}
                ${o.attributes.generation ? attrChip('gen', o.attributes.generation) : ''}
                <span class="attr"><span class="av">${money(o.price)}</span></span>
              </span>
            </div>`).join('')}
        </div>`).join('')}
    </div>`;
  }).join('');
}

function attrChip(name, value) {
  const isNull = value === null || value === undefined || value === '';
  return `<span class="attr ${isNull ? 'null' : ''}">
    <span class="an">${esc(name)}</span><span class="av">${esc(isNull ? 'none' : value)}</span></span>`;
}

// ============================================================ PRICE HISTORY

async function loadTrackedItems() {
  const list = $('#itemList');
  loading(list, 'loading…');
  try {
    const items = await api('/tracked-items?min_observations=5');
    state.pricesLoaded = true;

    if (!items.length) {
      list.innerHTML = `<div class="empty" style="padding:20px 10px">
        No price history yet.<br><br>Run:<br>
        <code>python -m scripts.backfill_prices --days 180</code></div>`;
      return;
    }

    $('#itemCount').textContent = `${items.length} items`;
    list.innerHTML = items.map((it, i) => `
      <div class="item" data-source="${esc(it.source)}" data-id="${esc(it.source_item_id)}">
        <div class="it" title="${esc(it.title)}">${esc(it.title || it.source_item_id)}</div>
        <div class="im">${esc(it.source)} · ${it.observations} obs · ${money(it.min_price)}–${money(it.max_price)}</div>
      </div>`).join('');

    $$('.item', list).forEach(el => {
      el.addEventListener('click', () => {
        $$('.item', list).forEach(x => x.classList.toggle('active', x === el));
        loadSeries(el.dataset.source, el.dataset.id);
      });
    });

    // Open the first item so the tab is never an empty chart.
    $$('.item', list)[0].click();
  } catch (err) {
    showError(list, err);
  }
}

async function loadSeries(source, itemId) {
  const chart = $('#chart');
  chart.innerHTML = `<div class="chart-empty"><span class="spinner"></span>loading series…</div>`;
  try {
    const d = await api(`/price-history/${encodeURIComponent(source)}/${encodeURIComponent(itemId)}`);
    $('#chartTitle').textContent = d.title || itemId;
    $('#chartMeta').textContent =
      `${d.source} · ${d.observations} observations` +
      (d.is_synthetic ? ' · SIMULATED data' : ' · real market data');

    chart.innerHTML = lineChart(d.series);

    const first = d.series[0].price, last = d.series[d.series.length - 1].price;
    const change = ((last - first) / first * 100);
    const spread = (d.max_price - d.min_price) / d.avg_price * 100;

    $('#chartStats').innerHTML = `
      ${stat(money(last), 'latest')}
      ${stat(money(d.min_price), 'lowest')}
      ${stat(money(d.max_price), 'highest')}
      ${stat((change >= 0 ? '+' : '') + change.toFixed(1) + '%', 'since start')}
      ${stat(spread.toFixed(1) + '%', 'spread of mean')}
      ${stat(d.observations, 'data points')}
    `;
  } catch (err) {
    showError(chart, err);
  }
}

/** Inline SVG line chart. No charting library, because a CDN dependency would
 *  break the offline requirement and a bundled one is 200KB for one line. */
function lineChart(series) {
  const W = 860, H = 340, P = { t: 18, r: 18, b: 34, l: 66 };
  const iw = W - P.l - P.r, ih = H - P.t - P.b;

  const prices = series.map(p => p.price);
  const lo = Math.min(...prices), hi = Math.max(...prices);
  const pad = (hi - lo) * 0.08 || hi * 0.02 || 1;
  const yMin = lo - pad, yMax = hi + pad;

  const x = i => P.l + (series.length === 1 ? iw / 2 : (i / (series.length - 1)) * iw);
  const y = v => P.t + ih - ((v - yMin) / (yMax - yMin)) * ih;

  const line = series.map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(p.price).toFixed(1)}`).join(' ');
  const area = `${line} L${x(series.length - 1).toFixed(1)},${(P.t + ih).toFixed(1)} L${x(0).toFixed(1)},${(P.t + ih).toFixed(1)} Z`;

  // y gridlines
  const ticks = 5;
  let grid = '';
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (yMax - yMin) * (i / ticks), yy = y(v);
    grid += `<line x1="${P.l}" y1="${yy.toFixed(1)}" x2="${W - P.r}" y2="${yy.toFixed(1)}"
                   stroke="#232c38" stroke-width="1"/>
             <text x="${P.l - 9}" y="${(yy + 4).toFixed(1)}" text-anchor="end"
                   fill="#6b7885" font-size="11" font-family="ui-monospace,monospace">${nf0.format(Math.round(v))}</text>`;
  }

  // x labels: about six evenly spaced dates
  let xlab = '';
  const step = Math.max(1, Math.floor(series.length / 6));
  for (let i = 0; i < series.length; i += step) {
    xlab += `<text x="${x(i).toFixed(1)}" y="${H - 12}" text-anchor="middle"
                   fill="#6b7885" font-size="10.5" font-family="ui-monospace,monospace">${esc(series[i].observed_on.slice(5))}</text>`;
  }

  const minI = prices.indexOf(lo), maxI = prices.indexOf(hi);

  return `<svg viewBox="0 0 ${W} ${H}" role="img"
    aria-label="Price over ${series.length} observations, from ${nf0.format(lo)} to ${nf0.format(hi)}">
    <defs>
      <linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%"   stop-color="#4c8dff" stop-opacity="0.28"/>
        <stop offset="100%" stop-color="#4c8dff" stop-opacity="0"/>
      </linearGradient>
    </defs>
    ${grid}${xlab}
    <path d="${area}" fill="url(#fill)"/>
    <path d="${line}" fill="none" stroke="#4c8dff" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${x(minI).toFixed(1)}" cy="${y(lo).toFixed(1)}" r="4" fill="#3fb950"/>
    <circle cx="${x(maxI).toFixed(1)}" cy="${y(hi).toFixed(1)}" r="4" fill="#f85149"/>
    <circle cx="${x(series.length - 1).toFixed(1)}" cy="${y(prices[prices.length - 1]).toFixed(1)}"
            r="4.5" fill="#4c8dff" stroke="#0e1117" stroke-width="2"/>
  </svg>`;
}

// ============================================================ SYSTEM HEALTH

async function loadHealth() {
  const grid = $('#systemGrid');
  try {
    const h = await api('/health');
    updateStatusChip(h);

    // The System Health panel was removed from the UI. The status chip in the
    // header still needs /health, so this function must survive the panel's
    // absence -- otherwise deleting a view breaks startup, which is exactly the
    // kind of coupling that made earlier changes fragile.
    if (!grid) return;

    const ph = h.price_history;
    const flatBad = ph.flat_series > 0;

    grid.innerHTML = `
      <div class="panel">
        <div class="panel-head"><h2>Price history</h2></div>
        ${kv('observations', nf0.format(ph.observations))}
        ${kv('distinct days', ph.distinct_days)}
        ${kv('tracked items', ph.tracked_items)}
        ${kv('real observations', nf0.format(ph.real_observations),
             ph.real_observations > 0 ? 'good' : 'warn')}
        ${kv('synthetic observations', nf0.format(ph.synthetic_observations))}
        ${kv('flat (unforecastable)', ph.flat_series, flatBad ? 'bad' : 'good')}
        ${kv('range', `${ph.first_observed ?? '—'} → ${ph.last_observed ?? '—'}`)}
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Sources</h2></div>
        ${h.adapters.map(a => kv(
          a.name,
          `${a.healthy ? 'healthy' : 'unreachable'}${a.synthetic ? ' · simulated' : ''}`,
          a.healthy ? (a.synthetic ? 'warn' : 'good') : 'bad')).join('')}
        ${kv('embedding backend', h.embedder_backend)}
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Cache &amp; currency</h2></div>
        ${kv('cache entries', h.cache.entries)}
        ${kv('oldest entry', h.cache.oldest_age_seconds != null ? h.cache.oldest_age_seconds + 's' : '—')}
        ${kv('base currency', h.fx.base_currency)}
        ${kv('FX snapshot', h.fx.as_of)}
        ${kv('FX age', h.fx.age_days + ' days', h.fx.age_days > 90 ? 'warn' : 'good')}
        ${kv('version', h.version)}
      </div>

      <div class="panel">
        <div class="panel-head"><h2>Warnings</h2>
          <span class="hint">${h.warnings.length} active</span></div>
        <div class="warnlist">
          ${h.warnings.length
            ? h.warnings.map(w => `<div class="warnitem">${esc(w)}</div>`).join('')
            : `<div class="okitem">No warnings. Every check passed.</div>`}
        </div>
      </div>
    `;
    const raw = $('#healthRaw');
    if (raw) raw.textContent = JSON.stringify(h, null, 2);
  } catch (err) {
    if (grid) showError(grid, err,
      'Is the API running? <code>python -m scripts.serve</code>');
    updateStatusChip(null);
  }
}

const kv = (k, v, cls = '') =>
  `<div class="kv"><span class="k">${esc(k)}</span><span class="v ${cls}">${esc(v)}</span></div>`;

function updateStatusChip(h) {
  const chip = $('#statusChip'), text = $('#statusText');
  chip.className = 'status';
  if (!h) { chip.classList.add('error'); text.textContent = 'API unreachable'; chip.title = 'Start it with: python -m scripts.serve'; return; }
  chip.classList.add(h.status === 'ok' ? 'ok' : 'degraded');
  const live = h.adapters.some(a => !a.synthetic);
  text.textContent = `${h.status} · ${live ? 'live data' : 'simulated data'} · ${h.embedder_backend}`;
  chip.title = h.warnings.join('\n') || 'All checks passed';
}

// ============================================================ boot

(async function boot() {
  syncWeightLabels();
  await loadHealth();        // fills the status chip before the first search
  await loadCategories();    // browse bar, and reports catalogue size in the footer
  await runSearch();
})();
