/* ============================================================
   SF Apartment Dashboard — vanilla JS
   ============================================================ */

// ---------- Constants ----------

const NEIGHBORHOODS = [
  "Castro",
  "Eureka Valley",
  "Noe Valley",
  "Cole Valley",
  "NoPa",
  "Hayes Valley",
  "Duboce Triangle",
  "Mission",
];

const DEFAULT_WEIGHTS = {
  price: 20,
  neighborhood: 15,
  top_floor: 10,
  outdoor: 10,
  dog: 8,
  quiet_street: 7,
  rent_control: 10,
};

const WEIGHT_LABELS = {
  price: "Price fit",
  neighborhood: "Neighborhood",
  top_floor: "Top floor",
  outdoor: "Outdoor space",
  dog: "Dog-friendly",
  quiet_street: "Quiet side street",
  rent_control: "Likely RC",
};

const COLUMNS = [
  { key: "thumb",        label: "",       sort: null },
  { key: "score",        label: "Score",  sort: (a, b) => b._score - a._score },
  { key: "status",       label: "Stage",  sort: (a, b) => stageOrder(b.user_status) - stageOrder(a.user_status) },
  { key: "price",        label: "Price",  sort: (a, b) => a.price - b.price },
  { key: "ppsf",         label: "$/sqft", sort: (a, b) => (a.price_per_sqft || 99) - (b.price_per_sqft || 99) },
  { key: "bedrooms",     label: "BR",     sort: (a, b) => a.bedrooms - b.bedrooms },
  { key: "neighborhood", label: "Hood",   sort: (a, b) => a.neighborhood.localeCompare(b.neighborhood) },
  { key: "address",      label: "Address",sort: (a, b) => a.address.localeCompare(b.address) },
  { key: "laundry",      label: "Laundry",sort: (a, b) => (a.laundry || "").localeCompare(b.laundry || "") },
  { key: "parking",      label: "Parking",sort: (a, b) => (a.parking || "").localeCompare(b.parking || "") },
  { key: "top_floor",    label: "Top",    sort: (a, b) => (b.top_floor === true) - (a.top_floor === true) },
  { key: "outdoor",      label: "Outdoor",sort: (a, b) => (b.outdoor_space === true) - (a.outdoor_space === true) },
  { key: "dog",          label: "Dog",    sort: (a, b) => (b.dog_friendly === true) - (a.dog_friendly === true) },
  { key: "rc",           label: "RC",     sort: (a, b) => (b.likely_rent_controlled_score || 0) - (a.likely_rent_controlled_score || 0) },
  { key: "source",       label: "Source", sort: (a, b) => a.source.localeCompare(b.source) },
  { key: "dom",          label: "DoM",    sort: (a, b) => daysOnMarket(b) - daysOnMarket(a) },
  { key: "date_posted",  label: "Posted", sort: (a, b) => new Date(b.date_posted) - new Date(a.date_posted) },
  { key: "link",         label: "Link",   sort: null },
];

const STATUS_VALUES = [
  { v: null,         label: "—",          short: "" },
  { v: "interested", label: "Interested", short: "★" },
  { v: "toured",     label: "Toured",     short: "👁" },
  { v: "applied",    label: "Applied",    short: "✓" },
  { v: "passed",     label: "Passed",     short: "⊘" },
];

function stageOrder(s) {
  return { interested: 1, toured: 2, applied: 3, passed: -1, null: 0 }[s] || 0;
}

function daysOnMarket(L) {
  const first = new Date(L.date_first_seen || L.date_posted);
  const last = L.status === "inactive" ? new Date(L.date_last_seen) : new Date();
  return Math.max(0, Math.floor((last - first) / (1000 * 60 * 60 * 24)));
}

function priceChangeBadge(L) {
  // Use price_history if present; else compare to previous_price
  if (!L.price_history || L.price_history.length < 2) return "";
  const earliest = L.price_history[0].price;
  if (!earliest || earliest === L.price) return "";
  const delta = L.price - earliest;
  const pct = Math.abs(delta / earliest * 100).toFixed(0);
  if (delta < 0) return `<span class="price-delta good">↓ $${Math.abs(delta).toLocaleString()} (${pct}%)</span>`;
  if (delta > 0) return `<span class="price-delta bad">↑ $${delta.toLocaleString()}</span>`;
  return "";
}

const SF_CENTER = [37.7659, -122.4361]; // roughly the centroid of your 8 neighborhoods

// ---------- State ----------

const state = {
  data: null,                                // raw loaded JSON
  weights: loadWeights(),                    // user-tuned or defaults
  prefs: loadPrefs(),                        // {starred, contacted, hidden, custom}
  tab: "active",
  sort: { col: "score", dir: "desc" },
  filters: {
    search: "",
    neighborhoods: new Set(NEIGHBORHOODS),
    priceMin: 0,
    priceMax: 6500,
    bedsMin: 2,
    hideNoParking: false,
    hideNoLaundry: false,
    onlyNew: false,
    hideHidden: true,
  },
  selectedId: null,
  map: null,
  markers: {},
  scoreChart: null,
  galleryIdx: 0,
};

// ---------- localStorage helpers ----------

function loadWeights() {
  try {
    const w = JSON.parse(localStorage.getItem("sf-weights"));
    if (w && typeof w === "object") return { ...DEFAULT_WEIGHTS, ...w };
  } catch {}
  return { ...DEFAULT_WEIGHTS };
}
function saveWeights() {
  localStorage.setItem("sf-weights", JSON.stringify(state.weights));
}

function loadPrefs() {
  try {
    const p = JSON.parse(localStorage.getItem("sf-prefs"));
    if (p) return {
      starred: new Set(p.starred || []),
      hidden:  new Set(p.hidden  || []),
      notes:   p.notes  || {},   // {listingId: string}
      status:  p.status || {},   // {listingId: "interested"|"toured"|"applied"|"passed"}
    };
  } catch {}
  return { starred: new Set(), hidden: new Set(), notes: {}, status: {} };
}
function savePrefs() {
  localStorage.setItem("sf-prefs", JSON.stringify({
    starred:  [...state.prefs.starred],
    hidden:   [...state.prefs.hidden],
    notes:    state.prefs.notes,
    status:   state.prefs.status,
  }));
}

// ---------- Scoring ----------

function scoreListing(L, w) {
  // Each component is normalized to its weight.
  const breakdown = {};

  // Price: linear from 6500 (=0) to 4000 or below (=full weight)
  if (L.price <= 4000) breakdown.price = w.price;
  else if (L.price >= 6500) breakdown.price = 0;
  else breakdown.price = ((6500 - L.price) / 2500) * w.price;

  // Neighborhood: full credit if in our 8 (which it should be — we filter elsewhere)
  breakdown.neighborhood = NEIGHBORHOODS.includes(L.neighborhood) ? w.neighborhood : 0;

  // Top floor: 100% confirmed, 50% ambiguous, 0 confirmed not
  if (L.top_floor === true)       breakdown.top_floor = w.top_floor;
  else if (L.top_floor === false) breakdown.top_floor = 0;
  else                            breakdown.top_floor = w.top_floor * 0.5;

  // Outdoor space: confirmed only
  breakdown.outdoor = L.outdoor_space === true ? w.outdoor : 0;

  // Dog: 100% dog_ok, 50% unstated, 0 no_pets/cats_only
  if (L.pet_policy === "dog_ok")        breakdown.dog = w.dog;
  else if (L.pet_policy === "unstated") breakdown.dog = w.dog * 0.5;
  else                                  breakdown.dog = 0;

  // Quiet side street: 100% confirmed, ~43% uncertain, 0 on corridor
  if (L.side_street === true)       breakdown.quiet_street = w.quiet_street;
  else if (L.side_street === false) breakdown.quiet_street = 0;
  else                              breakdown.quiet_street = w.quiet_street * 0.43;

  // Rent control: scaled by likely_rent_controlled_score (0/5/10) → 0/50/100% of weight
  const rc = L.likely_rent_controlled_score;
  if (rc === 10)     breakdown.rent_control = w.rent_control;
  else if (rc === 5) breakdown.rent_control = w.rent_control * 0.5;
  else               breakdown.rent_control = 0;

  const total = Object.values(breakdown).reduce((s, v) => s + v, 0);
  return { score: Math.round(total * 10) / 10, breakdown };
}

function applyScores() {
  if (!state.data) return;
  for (const L of state.data.listings) {
    const { score, breakdown } = scoreListing(L, state.weights);
    L._score = score;
    L._breakdown = breakdown;
  }
}

// ---------- Color scale ----------

function scoreColor(score) {
  // green at max weight total, red at 0; HSL hue 0-120
  const max = Object.values(state.weights).reduce((s, v) => s + v, 0) || 1;
  const t = Math.max(0, Math.min(1, score / max));
  const hue = t * 120; // 0 = red, 120 = green
  return `hsl(${hue}, 60%, 42%)`;
}

// ---------- Filtering ----------

function filterListings(rows) {
  const f = state.filters;
  const q = f.search.trim().toLowerCase();
  return rows.filter(L => {
    if (q && !(L.title.toLowerCase().includes(q) ||
               (L.description_snippet || "").toLowerCase().includes(q) ||
               (L.address || "").toLowerCase().includes(q))) return false;
    if (!f.neighborhoods.has(L.neighborhood)) return false;
    if (L.price < f.priceMin || L.price > f.priceMax) return false;
    if (L.bedrooms < f.bedsMin) return false;
    if (f.hideNoParking && (L.parking === null || L.parking === undefined || L.parking === "unknown")) return false;
    if (f.hideNoLaundry && (L.laundry === null || L.laundry === undefined || L.laundry === "unknown")) return false;
    if (f.onlyNew && !L.is_new_since_last_refresh) return false;
    if (f.hideHidden && state.prefs.hidden.has(L.id)) return false;
    return true;
  });
}

function listingsByTab() {
  if (!state.data) return [];
  return state.data.listings.filter(L => L.status === state.tab);
}

// ---------- Rendering: table ----------

function fmtMoney(n) {
  if (n == null) return "—";
  return "$" + Math.round(n).toLocaleString();
}

function fmtDate(s) {
  if (!s) return "—";
  const d = new Date(s);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function bool(v) {
  if (v === true)  return `<span class="bool-y">●</span>`;
  if (v === false) return `<span class="bool-n">○</span>`;
  return `<span class="bool-q">?</span>`;
}

function laundryCell(L) {
  if (L.laundry === "in_unit")     return `<span class="tag good">in-unit</span>`;
  if (L.laundry === "in_building") return `<span class="tag">building</span>`;
  if (L.laundry === "none")        return `<span class="tag bad">none</span>`;
  return `<span class="tag warn">?</span>`;
}

function parkingCell(L) {
  if (L.parking === "garage")   return `<span class="tag good">garage</span>`;
  if (L.parking === "driveway") return `<span class="tag good">driveway</span>`;
  if (L.parking === "deeded")   return `<span class="tag good">deeded</span>`;
  if (L.parking === "street")   return `<span class="tag bad">street</span>`;
  if (L.parking === "none")     return `<span class="tag bad">none</span>`;
  return `<span class="tag warn">?</span>`;
}

function rcCell(L) {
  const rc = L.likely_rent_controlled_score;
  if (rc === 10) return `<span class="tag good">RC</span>`;
  if (rc === 5)  return `<span class="tag warn">~RC</span>`;
  return `<span class="tag bad">no</span>`;
}

function renderHeader() {
  const tr = document.getElementById("th-row");
  tr.innerHTML = COLUMNS.map(c => {
    const isSorted = state.sort.col === c.key;
    const arrow = isSorted ? (state.sort.dir === "asc" ? "▲" : "▼") : "▾";
    return `<th data-col="${c.key}" class="${isSorted ? "sorted" : ""}">
              ${c.label}${c.sort ? `<span class="sort-arrow">${arrow}</span>` : ""}
            </th>`;
  }).join("");
  tr.querySelectorAll("th").forEach(th => {
    th.onclick = () => {
      const col = th.dataset.col;
      const colDef = COLUMNS.find(c => c.key === col);
      if (!colDef.sort) return;
      if (state.sort.col === col) state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
      else { state.sort.col = col; state.sort.dir = "desc"; }
      render();
    };
  });
}

function renderTable() {
  const body = document.getElementById("listings-body");
  const empty = document.getElementById("empty-msg");
  const colDef = COLUMNS.find(c => c.key === state.sort.col);

  let rows = filterListings(listingsByTab());
  if (colDef && colDef.sort) {
    rows.sort(colDef.sort);
    if (state.sort.dir === "asc") rows.reverse();
  }

  if (rows.length === 0) {
    body.innerHTML = "";
    empty.style.display = "flex";
  } else {
    empty.style.display = "none";
    body.innerHTML = rows.map(L => {
      const isSel = L.id === state.selectedId;
      const isStar = state.prefs.starred.has(L.id);
      const isHidden = state.prefs.hidden.has(L.id);
      const thumbUrl = (L.photos && L.photos.length) ? L.photos[0] : null;
      const thumbHtml = thumbUrl
        ? `<div class="thumb" style="background-image:url('${thumbUrl}')"></div>`
        : `<div class="thumb no-photo">⌂</div>`;
      const userStatus = state.prefs.status[L.id] || null;
      const statusDef = STATUS_VALUES.find(s => s.v === userStatus) || STATUS_VALUES[0];
      const dom = daysOnMarket(L);
      const domClass = dom > 14 ? "warn" : "";
      const priceBadge = priceChangeBadge(L);
      const gemBadge = L.is_hidden_gem ? `<span class="gem-badge" title="Hidden gem: pre-1979 multi-unit + below-median price">💎 GEM</span>` : "";
      const belowBadge = (L.below_market_pct || 0) >= 10 ? `<span class="bm-badge" title="${L.below_market_pct}% below ${L.neighborhood} median $/sqft">↓ ${L.below_market_pct.toFixed(0)}%</span>` : "";
      const adjBadge = L.status === "adjacent" ? `<span class="adj-badge" title="Outside your 8 target nbhds, but a close-by neighborhood">${L.neighborhood}</span>` : "";
      return `<tr data-id="${L.id}"
                  class="${isSel ? "selected" : ""} ${isStar ? "starred" : ""} ${isHidden ? "hidden-row" : ""} ${userStatus ? "stage-" + userStatus : ""}">
        <td class="thumb-cell">${thumbHtml}</td>
        <td><span class="score-cell" style="background:${scoreColor(L._score)}">${L._score.toFixed(0)}</span>${gemBadge}</td>
        <td class="status-cell">${userStatus ? `<span class="stage-pill ${userStatus}">${statusDef.short} ${statusDef.label}</span>` : ""}</td>
        <td class="price-cell">${fmtMoney(L.price)}${belowBadge ? " " + belowBadge : ""}${priceBadge ? "<br>" + priceBadge : ""}</td>
        <td class="ppsf-cell">${L.price_per_sqft ? "$" + L.price_per_sqft.toFixed(2) : "—"}</td>
        <td class="num-cell">${L.bedrooms}</td>
        <td>${L.neighborhood}${L.neighborhood_confidence === "low" ? ` <span class="dim mono">?</span>` : ""}</td>
        <td title="${L.address}">${L.address || "—"}</td>
        <td>${laundryCell(L)}</td>
        <td>${parkingCell(L)}</td>
        <td style="text-align:center">${bool(L.top_floor)}</td>
        <td style="text-align:center">${bool(L.outdoor_space)}</td>
        <td style="text-align:center">${bool(L.dog_friendly)}</td>
        <td>${rcCell(L)}</td>
        <td><span class="source-badge ${L.source}">${L.source}</span></td>
        <td class="num-cell ${domClass}">${dom}d</td>
        <td class="mono dim">${fmtDate(L.date_posted)}</td>
        <td><a href="${L.source_url}" target="_blank" rel="noopener">↗</a></td>
        ${state.tab === "excluded" ? `<td class="dim" style="font-size:10px">${L.exclusion_reason || ""}</td>` : ""}
      </tr>`;
    }).join("");

    body.querySelectorAll("tr").forEach(tr => {
      tr.onclick = (e) => {
        if (e.target.tagName === "A") return;
        selectListing(tr.dataset.id);
      };
    });
  }

  // Add/remove an excluded-reason column header dynamically
  let th = document.getElementById("th-excl");
  if (state.tab === "excluded" && !th) {
    th = document.createElement("th");
    th.id = "th-excl";
    th.textContent = "Reason";
    document.getElementById("th-row").appendChild(th);
  } else if (state.tab !== "excluded" && th) {
    th.remove();
  }
}

// ---------- Rendering: map ----------

function initMap() {
  state.map = L.map("map", { zoomControl: true, attributionControl: false }).setView(SF_CENTER, 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
  }).addTo(state.map);
}

function renderMap() {
  // Clear old markers
  for (const id in state.markers) {
    state.map.removeLayer(state.markers[id]);
  }
  state.markers = {};

  const rows = filterListings(listingsByTab());
  rows.forEach(L => {
    if (L.lat == null || L.lng == null) return;
    const color = scoreColor(L._score);
    const isSel = L.id === state.selectedId;
    const m = window.L.circleMarker([L.lat, L.lng], {
      radius: isSel ? 9 : 6,
      color: isSel ? "#fff" : color,
      weight: isSel ? 2 : 1,
      fillColor: color,
      fillOpacity: 0.85,
    });
    const popPhoto = (L.photos && L.photos[0])
      ? `<div class="pop-photo" style="background-image:url('${L.photos[0]}')"></div>`
      : "";
    m.bindPopup(`
      <div class="map-popup">
        ${popPhoto}
        <div style="font-weight:600">${escapeHtml(L.title)}</div>
        <div class="dim" style="font-size:11px;margin-top:2px">${L.neighborhood} · <span class="mono">${fmtMoney(L.price)}</span> · ${L.bedrooms}BR</div>
        <div style="margin-top:6px;display:flex;gap:6px;align-items:center">
          <span style="background:${color};padding:2px 8px;border-radius:10px;color:#fff;font-family:var(--mono);font-size:11px;font-weight:700">${L._score.toFixed(0)}</span>
          <a href="${L.source_url}" target="_blank" rel="noopener" style="font-size:11px">view ↗</a>
        </div>
      </div>
    `, { maxWidth: 240 });
    m.on("click", () => selectListing(L.id));
    m.addTo(state.map);
    state.markers[L.id] = m;
  });
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

// ---------- Detail panel ----------

function selectListing(id) {
  if (state.selectedId !== id) state.galleryIdx = 0;
  state.selectedId = id;
  renderTable();
  renderMap();
  renderDetail();
  // Scroll to row
  const row = document.querySelector(`tr[data-id="${id}"]`);
  if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  // Pan map
  const L = state.data.listings.find(x => x.id === id);
  if (L && L.lat != null && state.map) {
    state.map.panTo([L.lat, L.lng], { animate: true });
    if (state.markers[id]) state.markers[id].openPopup();
  }
}

function renderGallery(L) {
  const host = document.getElementById("d-gallery");
  if (!host) return;
  const photos = L.photos || [];
  if (photos.length === 0) {
    host.className = "gallery empty";
    host.innerHTML = `<span>No photos available</span>`;
    return;
  }
  host.className = "gallery";
  if (state.galleryIdx >= photos.length) state.galleryIdx = 0;
  const idx = state.galleryIdx;

  const photoLayers = photos.map((p, i) =>
    `<div class="photo ${i === idx ? "active" : ""}" style="background-image:url('${p}')"></div>`
  ).join("");

  const hasMultiple = photos.length > 1;
  const navHtml = hasMultiple
    ? `<button class="nav prev" id="gallery-prev" title="Previous">‹</button>
       <button class="nav next" id="gallery-next" title="Next">›</button>
       <div class="indicator">
         ${photos.map((_, i) => `<div class="dot ${i === idx ? "active" : ""}" data-idx="${i}"></div>`).join("")}
       </div>
       <div class="photo-counter">${idx + 1} / ${photos.length}</div>`
    : "";

  host.innerHTML = photoLayers + navHtml;

  if (hasMultiple) {
    const total = photos.length;
    document.getElementById("gallery-prev").onclick = (e) => {
      e.stopPropagation();
      state.galleryIdx = (idx - 1 + total) % total;
      renderGallery(L);
    };
    document.getElementById("gallery-next").onclick = (e) => {
      e.stopPropagation();
      state.galleryIdx = (idx + 1) % total;
      renderGallery(L);
    };
    host.querySelectorAll(".indicator .dot").forEach(dot => {
      dot.onclick = (e) => {
        e.stopPropagation();
        state.galleryIdx = +dot.dataset.idx;
        renderGallery(L);
      };
    });
  }
}

function renderDetail() {
  const panel = document.getElementById("detail-panel");
  if (!state.selectedId) { panel.classList.remove("open"); return; }
  const L = state.data.listings.find(x => x.id === state.selectedId);
  if (!L) { panel.classList.remove("open"); return; }
  panel.classList.add("open");

  renderGallery(L);

  document.getElementById("d-title").textContent = L.title;
  document.getElementById("d-sub").innerHTML =
    `${L.neighborhood} · <span class="price">${fmtMoney(L.price)}</span> · ${L.bedrooms}BR/${L.bathrooms || "?"}BA · ${L.sqft || "?"} sqft` +
    (L.cross_posted_on?.length ? ` · also on ${L.cross_posted_on.join(", ")}` : "");

  const body = document.getElementById("d-body");
  // Google Maps + Street View links — uses address if available, else lat/lng.
  const mapQuery = encodeURIComponent(L.address ? `${L.address}, San Francisco, CA` : `${L.lat},${L.lng}`);
  const streetViewUrl = `https://www.google.com/maps?q=&layer=c&cbll=${L.lat || ""},${L.lng || ""}`;
  const mapsLinkUrl  = `https://www.google.com/maps/search/?api=1&query=${mapQuery}`;
  const mapEmbedUrl  = `https://maps.google.com/maps?q=${mapQuery}&z=18&t=k&output=embed`;
  const exteriorBlock = (L.address || (L.lat && L.lng))
    ? `<h5>Building & street view</h5>
       <div class="map-embed">
         <iframe src="${mapEmbedUrl}" loading="lazy" referrerpolicy="no-referrer-when-downgrade"
                 width="100%" height="180" frameborder="0" allowfullscreen></iframe>
         <div class="map-links">
           <a href="${streetViewUrl}" target="_blank" rel="noopener">📸 Street view</a>
           <a href="${mapsLinkUrl}" target="_blank" rel="noopener">🗺 Open in Maps</a>
         </div>
       </div>`
    : "";

  body.innerHTML = `
    <h5>Description snippet</h5>
    <div class="desc">${escapeHtml(L.description_snippet || "(no snippet)")}</div>

    ${exteriorBlock}

    <h5>Score breakdown <span class="dim mono">total ${L._score.toFixed(1)}</span></h5>
    <div class="score-bars" id="d-bars"></div>

    <h5>Details</h5>
    <div class="grid">
      <div class="k">Address</div>      <div class="v">${escapeHtml(L.address)}</div>
      <div class="k">Source</div>       <div class="v"><span class="source-badge ${L.source}">${L.source}</span></div>
      <div class="k">Posted</div>       <div class="v">${fmtDate(L.date_posted)} (${L.times_seen}× seen)</div>
      <div class="k">First seen</div>   <div class="v">${fmtDate(L.date_first_seen)}</div>
      <div class="k">Last seen</div>    <div class="v">${fmtDate(L.date_last_seen)}</div>
      <div class="k">Year built</div>   <div class="v">${L.year_built ?? "—"}</div>
      <div class="k">Units</div>        <div class="v">${L.unit_count ?? "—"}</div>
      <div class="k">$/sqft</div>       <div class="v">${L.price_per_sqft ? "$" + L.price_per_sqft.toFixed(2) : "—"}</div>
      <div class="k">Top floor</div>    <div class="v">${L.top_floor === true ? "yes" : L.top_floor === false ? "no" : "?"}</div>
      <div class="k">Outdoor</div>      <div class="v">${L.outdoor_space === true ? "yes" : L.outdoor_space === false ? "no" : "?"}</div>
      <div class="k">Side street</div>  <div class="v">${L.side_street === true ? "yes" : L.side_street === false ? "no" : "?"}</div>
      <div class="k">Pet policy</div>   <div class="v">${L.pet_policy ?? "—"}</div>
      <div class="k">Laundry</div>      <div class="v">${L.laundry ?? "—"}</div>
      <div class="k">Parking</div>      <div class="v">${L.parking ?? "—"}</div>
    </div>

    <h5>Rent-control reasoning</h5>
    <div class="rc-reasoning">${escapeHtml(L.rc_reasoning || "(none)")}</div>

    <h5>My notes</h5>
    <textarea class="notes-area" id="d-notes" placeholder="What stood out? Tour scheduled? Agent contact?">${escapeHtml(state.prefs.notes[L.id] || "")}</textarea>

    ${L.price_history && L.price_history.length > 1 ? `
    <h5>Price history</h5>
    <div class="price-history">
      ${L.price_history.map(p => `<div class="ph-row"><span class="dim">${fmtDate(p.date)}</span><span class="mono">${fmtMoney(p.price)}</span></div>`).join("")}
    </div>` : ""}

    <h5>Original listing</h5>
    <a class="source-link" href="${L.source_url}" target="_blank" rel="noopener">↗ ${L.source_url}</a>
  `;

  // Notes auto-save (debounced)
  const notesEl = document.getElementById("d-notes");
  if (notesEl) {
    let timer;
    notesEl.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        state.prefs.notes[L.id] = notesEl.value;
        savePrefs();
      }, 400);
    };
  }
  // Status dropdown
  const statusEl = document.getElementById("d-status");
  if (statusEl) {
    statusEl.value = state.prefs.status[L.id] || "";
    statusEl.onchange = () => {
      const v = statusEl.value;
      if (v) state.prefs.status[L.id] = v;
      else delete state.prefs.status[L.id];
      savePrefs();
      renderTable();
    };
  }

  // Score breakdown bars
  const barsHost = document.getElementById("d-bars");
  const totalMax = Object.values(state.weights).reduce((s, x) => s + x, 0);
  barsHost.innerHTML = Object.entries(L._breakdown).map(([k, v]) => {
    const w = state.weights[k] || 0;
    const pct = w > 0 ? (v / w) * 100 : 0;
    const barColor = scoreColor((v / Math.max(w, 0.0001)) * totalMax);
    return `<div class="score-bar-row">
      <div class="lbl">${WEIGHT_LABELS[k] || k}</div>
      <div class="bar"><div class="fill" style="width:${pct.toFixed(1)}%; background:${barColor}"></div></div>
      <div class="val">${v.toFixed(1)} <span class="max">/${w}</span></div>
    </div>`;
  }).join("");

  // Action buttons reflect state
  const star = document.getElementById("d-star");
  const hide = document.getElementById("d-hide");
  star.classList.toggle("active", state.prefs.starred.has(L.id));
  star.classList.toggle("starred", state.prefs.starred.has(L.id));
  hide.classList.toggle("active", state.prefs.hidden.has(L.id));
  hide.classList.toggle("hidden", state.prefs.hidden.has(L.id));
}

// ---------- Refresh log ----------

function renderRefreshLog() {
  const host = document.getElementById("refresh-log-pane");
  if (!state.data || !state.data.refresh_log) {
    host.innerHTML = `<div class="empty">No refresh history yet.</div>`;
    return;
  }
  host.innerHTML = state.data.refresh_log.map(entry => {
    const srcs = Object.entries(entry.sources).map(([k, v]) =>
      `${k}: <b>${v.pulled}</b>${v.errors ? ` <span style="color:var(--bad)">(${v.errors} err)</span>` : ""}`
    ).join(" · ");
    const t = entry.totals || {};
    const errs = (entry.errors || []).map(e => `<div class="err">⚠ ${e.source}: ${escapeHtml(e.message)}</div>`).join("");
    return `<div class="entry">
      <div class="ts">${new Date(entry.timestamp).toLocaleString()}</div>
      <div class="row">${srcs} · <span class="dim">${entry.duration_seconds}s</span></div>
      <div class="row">
        <span><b style="color:var(--new)">+${t.new || 0}</b> new</span>
        <span><b>${t.updated || 0}</b> updated</span>
        <span><b style="color:var(--bad)">${t.newly_inactive || 0}</b> newly inactive</span>
        <span class="dim">→ ${t.active_total || 0} active / ${t.excluded_total || 0} excluded / ${t.inactive_total || 0} inactive</span>
      </div>
      ${errs}
    </div>`;
  }).join("");
}

// ---------- Top-bar summary ----------

function renderSummary() {
  const all = state.data?.listings || [];
  const counts = { active: 0, excluded: 0, inactive: 0, adjacent: 0, new: 0 };
  for (const L of all) {
    if (L.status in counts) counts[L.status]++;
    if (L.is_new_since_last_refresh) counts.new++;
  }
  document.getElementById("stat-active").textContent = counts.active;
  document.getElementById("stat-new").textContent = counts.new;
  document.getElementById("stat-excluded").textContent = counts.excluded;
  document.getElementById("stat-inactive").textContent = counts.inactive;

  document.getElementById("tab-active-count").textContent = counts.active;
  const tabAdj = document.getElementById("tab-adjacent-count"); if (tabAdj) tabAdj.textContent = counts.adjacent;
  document.getElementById("tab-excluded-count").textContent = counts.excluded;
  document.getElementById("tab-inactive-count").textContent = counts.inactive;

  document.getElementById("last-refresh").textContent =
    "last refresh: " + (state.data?.last_refresh ? new Date(state.data.last_refresh).toLocaleString() : "—");
}

// ---------- Top-level render ----------

function render() {
  applyScores();
  renderHeader();
  renderTable();
  renderMap();
  renderDetail();
  renderSummary();
}

// ---------- Tabs ----------

function setTab(tab) {
  state.tab = tab;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
  const tableWrap = document.getElementById("table-wrap");
  const logPane = document.getElementById("refresh-log-pane");
  if (tab === "log") {
    tableWrap.style.display = "none";
    logPane.style.display = "block";
    renderRefreshLog();
  } else {
    tableWrap.style.display = "block";
    logPane.style.display = "none";
    render();
  }
}

// ---------- Filter wiring ----------

function buildNeighborhoodPicker() {
  const popup = document.getElementById("f-nh-popup");
  popup.innerHTML = NEIGHBORHOODS.map(n => `
    <label><input type="checkbox" data-nh="${n}" ${state.filters.neighborhoods.has(n) ? "checked" : ""}/> ${n}</label>
  `).join("") + `
    <div style="border-top:1px solid var(--border);margin-top:6px;padding-top:6px;display:flex;gap:8px;font-size:10px">
      <a href="#" id="nh-all">All</a>
      <a href="#" id="nh-none">None</a>
    </div>
  `;
  popup.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.onchange = () => {
      if (cb.checked) state.filters.neighborhoods.add(cb.dataset.nh);
      else state.filters.neighborhoods.delete(cb.dataset.nh);
      updateNhTriggerLabel();
      render();
    };
  });
  popup.querySelector("#nh-all").onclick = (e) => {
    e.preventDefault();
    NEIGHBORHOODS.forEach(n => state.filters.neighborhoods.add(n));
    buildNeighborhoodPicker(); render();
  };
  popup.querySelector("#nh-none").onclick = (e) => {
    e.preventDefault();
    state.filters.neighborhoods.clear();
    buildNeighborhoodPicker(); render();
  };
  updateNhTriggerLabel();
}

function updateNhTriggerLabel() {
  const trig = document.getElementById("f-nh-trigger");
  const n = state.filters.neighborhoods.size;
  if (n === NEIGHBORHOODS.length) trig.innerHTML = `All <span class="dim">(${n})</span>`;
  else if (n === 0) trig.innerHTML = `<span style="color:var(--bad)">None</span>`;
  else trig.innerHTML = `<span class="mono">${n}</span> selected`;
}

// ---------- Weights panel ----------

function buildWeightsPanel() {
  const host = document.getElementById("weights-sliders");
  host.innerHTML = Object.entries(DEFAULT_WEIGHTS).map(([k, defVal]) => `
    <div class="slider-row">
      <label>
        <span>${WEIGHT_LABELS[k]}</span>
        <input type="range" min="0" max="30" step="1" value="${state.weights[k]}" data-w="${k}" />
      </label>
      <div class="val" data-wval="${k}">${state.weights[k]}</div>
    </div>
  `).join("");
  host.querySelectorAll("input[type=range]").forEach(input => {
    input.oninput = () => {
      state.weights[input.dataset.w] = +input.value;
      host.querySelector(`[data-wval="${input.dataset.w}"]`).textContent = input.value;
      saveWeights();
      render();
    };
  });
  document.getElementById("weights-reset").onclick = () => {
    state.weights = { ...DEFAULT_WEIGHTS };
    saveWeights();
    buildWeightsPanel();
    render();
  };
}

// ---------- Theme ----------

function setTheme(t) {
  document.documentElement.dataset.theme = t;
  localStorage.setItem("sf-theme", t);
}

// ---------- Init ----------

async function init() {
  // Theme
  const savedTheme = localStorage.getItem("sf-theme") || "dark";
  setTheme(savedTheme);

  // Load data
  try {
    const res = await fetch("../data/listings.json?_=" + Date.now());
    state.data = await res.json();
  } catch (e) {
    console.error("Failed to load listings.json", e);
    document.body.innerHTML = `<div style="padding:40px;color:#f55;font-family:monospace">Failed to load data/listings.json — open this page from a static server (e.g. <code>python -m http.server</code> from the project root) to avoid CORS, or use Safari/Firefox which permit local fetches. Detail: ${e.message}</div>`;
    return;
  }

  // Build UI shell
  initMap();
  buildNeighborhoodPicker();
  buildWeightsPanel();

  // Wire filters
  const f = state.filters;
  const wire = (id, fn) => document.getElementById(id).addEventListener("input", e => { fn(e); render(); });
  wire("f-search",       e => f.search = e.target.value);
  wire("f-price-min",    e => f.priceMin = +e.target.value || 0);
  wire("f-price-max",    e => f.priceMax = +e.target.value || 99999);
  wire("f-beds",         e => f.bedsMin = +e.target.value);
  wire("f-hide-no-parking", e => f.hideNoParking = e.target.checked);
  wire("f-hide-no-laundry", e => f.hideNoLaundry = e.target.checked);
  wire("f-only-new",     e => f.onlyNew = e.target.checked);
  wire("f-hide-hidden",  e => f.hideHidden = e.target.checked);

  // Neighborhood popup toggle
  const nhTrig = document.getElementById("f-nh-trigger");
  const nhPop = document.getElementById("f-nh-popup");
  nhTrig.onclick = (e) => { e.stopPropagation(); nhPop.classList.toggle("open"); };
  document.addEventListener("click", e => {
    if (!nhPop.contains(e.target) && !nhTrig.contains(e.target)) nhPop.classList.remove("open");
  });

  // Tabs
  document.querySelectorAll(".tab").forEach(t => {
    t.onclick = () => setTab(t.dataset.tab);
  });

  // Detail panel close
  document.getElementById("d-close").onclick = () => {
    state.selectedId = null;
    renderDetail();
    renderTable();
    renderMap();
  };

  // Action buttons
  function toggleSet(setName, btnId, cls) {
    document.getElementById(btnId).onclick = () => {
      const id = state.selectedId;
      if (!id) return;
      const s = state.prefs[setName];
      if (s.has(id)) s.delete(id); else s.add(id);
      savePrefs();
      renderDetail();
      renderTable();
    };
  }
  toggleSet("starred", "d-star", "starred");
  toggleSet("hidden", "d-hide", "hidden");

  // Refresh button — try POST /api/refresh first; if that fails (static-only mode),
  // fall back to a modal showing the terminal command.
  const modal = document.getElementById("refresh-modal");
  const refreshBtn = document.getElementById("refresh-btn");

  async function liveRefresh() {
    const origLabel = refreshBtn.innerHTML;
    refreshBtn.disabled = true;
    refreshBtn.innerHTML = "↻ Refreshing…";
    refreshBtn.classList.add("primary");
    try {
      const res = await fetch("/api/refresh", { method: "POST" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "refresh failed");
      // Reload listings.json
      const r2 = await fetch("../data/listings.json?_=" + Date.now());
      state.data = await r2.json();
      // Reset gallery & selection state
      state.galleryIdx = 0;
      render();
      // Briefly show the result
      const t = data.log_entry.totals;
      refreshBtn.innerHTML = `✓ +${t.new} new · ${t.active_total} active`;
      setTimeout(() => { refreshBtn.innerHTML = origLabel; refreshBtn.classList.remove("primary"); }, 4000);
    } catch (err) {
      // Static-mode fallback
      refreshBtn.innerHTML = origLabel;
      refreshBtn.classList.remove("primary");
      modal.classList.add("open");
    } finally {
      refreshBtn.disabled = false;
    }
  }

  refreshBtn.onclick = liveRefresh;
  document.getElementById("refresh-modal-close").onclick = () => modal.classList.remove("open");
  modal.onclick = (e) => { if (e.target === modal) modal.classList.remove("open"); };

  // Theme toggle
  document.getElementById("theme-btn").onclick = () => {
    const cur = document.documentElement.dataset.theme;
    setTheme(cur === "dark" ? "light" : "dark");
  };

  // First render
  render();
}

window.addEventListener("DOMContentLoaded", init);
