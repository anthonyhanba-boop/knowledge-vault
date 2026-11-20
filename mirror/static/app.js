// Project Mirror frontend (no build step). Talks only to the Mirror API; provider keys never reach the browser.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const view = $("#view");
const LABEL = { exact: "Exact match", near_duplicate: "Near-duplicate", cropped: "Cropped / partial", similar: "Similar" };
const ORDER = ["exact", "near_duplicate", "cropped", "similar"];
let pollTimer = null;
let providersCache = null;

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function safeHref(u) { try { const x = new URL(u); return /^https?:$/.test(x.protocol) ? x.href : null; } catch { return null; } }
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), 2600); }
function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now"; if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return new Date(iso).toLocaleDateString();
}
async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { }
    throw new Error(msg);
  }
  return r.status === 204 ? null : r.json();
}
async function getProviders(force) {
  if (!providersCache || force) providersCache = (await api("/api/providers")).providers;
  return providersCache;
}

// ---------------- router ----------------
async function route() {
  clearTimeout(pollTimer);
  const h = location.hash.slice(1) || "/";
  const [, page, id] = h.split("/");
  $$(".nav nav a").forEach(a => a.classList.toggle("active", a.dataset.nav === (page || "home")));
  if (page === "search" && id) return renderSearch(id);
  if (page === "history") return renderHistory();
  if (page === "saved") return renderSaved();
  if (page === "status") return renderStatus();
  return renderHome();
}
window.addEventListener("hashchange", route);

// ---------------- home ----------------
async function renderHome() {
  view.replaceChildren($("#tpl-home").content.cloneNode(true));
  const form = $("#searchForm"), input = $("#file");
  let file = null;

  const setFile = f => {
    if (!f) return;
    if (!/^image\/(jpeg|png|webp|gif|bmp)$/.test(f.type)) return toast("Use a JPG, PNG, WebP, GIF or BMP image");
    file = f;
    const url = URL.createObjectURL(f);
    $("#preview").src = url;
    $("#preview").onload = () => { $("#fmeta").textContent = `${$("#preview").naturalWidth}×${$("#preview").naturalHeight} · ${(f.size / 1024).toFixed(0)} KB · ${f.type.split("/")[1].toUpperCase()}`; };
    $("#fname").textContent = f.name || "Pasted image";
    $(".drop-empty", form).hidden = true; $(".drop-preview", form).hidden = false;
  };
  $("#browse").onclick = () => input.click();
  $(".drop-empty").onclick = e => { if (e.target.id !== "browse") input.click(); };
  input.onchange = () => setFile(input.files[0]);
  $("#clear").onclick = () => { file = null; input.value = ""; $(".drop-empty", form).hidden = false; $(".drop-preview", form).hidden = true; };
  $("#consent").onchange = e => { $("#go").disabled = !e.target.checked; };
  ["dragenter", "dragover"].forEach(ev => form.addEventListener(ev, e => { e.preventDefault(); form.classList.add("over"); }));
  ["dragleave", "drop"].forEach(ev => form.addEventListener(ev, e => { e.preventDefault(); form.classList.remove("over"); }));
  form.addEventListener("drop", e => setFile(e.dataTransfer.files[0]));
  document.onpaste = e => { const it = [...(e.clipboardData?.items || [])].find(i => i.type.startsWith("image/")); if (it && $("#searchForm")) setFile(it.getAsFile()); };

  form.onsubmit = async e => {
    e.preventDefault();
    if (!file || !$("#consent").checked) return;
    const btn = $("#go"); btn.disabled = true; btn.innerHTML = `<span class="spin"></span> Uploading…`;
    const fd = new FormData(); fd.append("file", file); fd.append("consent", "true");
    try { const s = await api("/api/searches", { method: "POST", body: fd }); location.hash = `#/search/${s.id}`; }
    catch (err) { toast(err.message); btn.disabled = false; btn.textContent = "Search the web"; }
  };

  // provider strip + config warning
  try {
    const ps = await getProviders(true);
    $("#providerStrip").innerHTML = ps.map(p => `<span class="pill"><span class="dot ${p.configured ? "ok" : ""}"></span>${esc(p.label)}${p.configured ? "" : " · not configured"}</span>`).join("");
    if (!ps.some(p => p.configured)) {
      $("#providerStrip").insertAdjacentHTML("afterend", `<div class="warnbox"><b>No search providers are configured.</b> Add an API key to <code>.env</code>, such as <code>GOOGLE_VISION_API_KEY</code>, then restart the server. See the <a href="#/status">Status page</a> for setup steps.</div>`);
    }
  } catch { }

  try {
    const { searches } = await api("/api/searches");
    $("#recent").innerHTML = searches.slice(0, 8).map(historyCard).join("") || `<div class="empty">Your searches appear here. Only this browser can see them.</div>`;
    bindHistoryDeletes();
  } catch { }
}

function historyCard(s) {
  const st = s.status === "done" ? `${s.result_count} results` : s.status === "failed" ? "failed" : "searching…";
  return `<a class="hcard" href="#/search/${s.id}">
    ${s.has_thumb ? `<img src="/api/searches/${s.id}/thumb" alt="" loading="lazy">` : `<div class="thumb"><span class="noimg">no preview</span></div>`}
    <div class="hb"><div>${esc(st)}</div><div class="muted">${ago(s.created_at)} · ${esc(s.filename)}</div></div>
    <button class="del" data-del="${s.id}" title="Delete search">✕</button></a>`;
}
function bindHistoryDeletes() {
  $$("[data-del]").forEach(b => b.onclick = async e => {
    e.preventDefault(); e.stopPropagation();
    if (!confirm("Delete this search, its results and its stored thumbnail?")) return;
    await api(`/api/searches/${b.dataset.del}`, { method: "DELETE" }); b.closest(".hcard").remove(); toast("Search deleted");
  });
}

// ---------------- history ----------------
async function renderHistory() {
  view.innerHTML = `<div class="page-head"><h1 class="page-title">Search history</h1>
    <button class="btn ghost danger" id="delAll">Delete all history</button></div><div class="recent-grid" id="hist"></div>`;
  const { searches } = await api("/api/searches");
  $("#hist").innerHTML = searches.map(historyCard).join("") || `<div class="empty">No searches yet.</div>`;
  bindHistoryDeletes();
  $("#delAll").onclick = async () => {
    if (!confirm("Permanently delete all searches, results, saved items and thumbnails?")) return;
    await api("/api/searches", { method: "DELETE" }); toast("History deleted"); renderHistory();
  };
}

// ---------------- saved ----------------
async function renderSaved() {
  view.innerHTML = `<div class="page-head"><h1 class="page-title">Saved results</h1></div><div id="results"></div>`;
  const { results } = await api("/api/saved");
  state = { search: null, results, fmts: new Set() };
  $("#results").innerHTML = results.length ? `<div class="grid">${results.map(card).join("")}</div>` : `<div class="empty">Save results with ☆ to keep them here.</div>`;
  bindCards();
}

// ---------------- status ----------------
async function renderStatus() {
  view.innerHTML = `<div class="page-head"><div><h1 class="page-title">Provider status</h1><div class="muted small">Health, latency and setup for each reverse-image-search provider</div></div>
    <button class="btn" id="check">Run live health check</button></div><div class="status-grid" id="sg"></div>
    <p class="muted small" style="margin-top:28px">Mirror only reports what the configured providers return. It does not scrape search engines and does not run facial recognition.</p>`;
  const draw = async (live = {}) => {
    const ps = await getProviders(true);
    $("#sg").innerHTML = ps.map(p => {
      const h = p.health || {}, l = live[p.name];
      const cls = !p.configured ? "" : l ? (l.ok ? "ok" : "bad") : h.last_error_at && (!h.last_ok_at || h.last_error_at > h.last_ok_at) ? "warn" : h.last_ok_at ? "ok" : "";
      return `<div class="scard"><h3><span class="dot ${cls}"></span>${esc(p.label)}</h3>
        <div class="muted small">${p.configured ? "Configured" : esc(p.config_hint)} · <a class="link" href="${esc(p.docs_url)}" target="_blank" rel="noopener">docs</a></div>
        <dl class="kv">
          ${l ? `<dt>Live check</dt><dd>${l.ok ? `OK · ${l.latency_ms} ms` : esc(l.error)}</dd>` : ""}
          <dt>Calls</dt><dd>${h.calls ?? 0} (${h.failures ?? 0} failed)</dd>
          <dt>Last latency</dt><dd>${h.last_latency_ms != null ? h.last_latency_ms + " ms" : "–"}</dd>
          <dt>Last success</dt><dd>${h.last_ok_at ? ago(h.last_ok_at) : "–"}</dd>
          <dt>Last error</dt><dd>${h.last_error ? esc(h.last_error) + " (" + ago(h.last_error_at) + ")" : "–"}</dd>
          <dt>Date data</dt><dd>${p.supports_dates ? "yes" : "no"}</dd>
        </dl></div>`;
    }).join("");
    updateNavDot(ps);
  };
  await draw();
  $("#check").onclick = async e => {
    e.target.disabled = true; e.target.innerHTML = `<span class="spin"></span> Checking…`;
    try { await draw(await api("/api/providers/check", { method: "POST" })); } finally { e.target.disabled = false; e.target.textContent = "Run live health check"; }
  };
}
function updateNavDot(ps) {
  const conf = ps.filter(p => p.configured);
  $("#navDot").className = "dot " + (!conf.length ? "bad" : conf.every(p => !p.health?.last_error_at || p.health.last_ok_at > p.health.last_error_at) ? "ok" : "warn");
}

// ---------------- search results ----------------
let state = { search: null, results: [], fmts: new Set() };

async function renderSearch(id) {
  view.replaceChildren($("#tpl-search").content.cloneNode(true));
  $("#results").innerHTML = `<div class="grid">${"<div class='skeleton'></div>".repeat(6)}</div>`;
  $("#qthumb").src = `/api/searches/${id}/thumb`;
  state = { search: null, results: [], fmts: new Set() };
  $("#filters").addEventListener("input", drawResults);
  $("#groupBy").onchange = drawResults;
  $("#exportBtn").onclick = () => { $("#exportMenu").hidden = !$("#exportMenu").hidden; };
  $$("#exportMenu a").forEach(a => a.onclick = () => {
    $("#exportMenu").hidden = true;
    location.href = `/api/searches/${id}/export?format=${a.dataset.fmt}`;
  });
  $("#retry").onclick = async () => {
    try { await api(`/api/searches/${id}/retry`, { method: "POST" }); toast("Retrying failed providers…"); poll(id); }
    catch (e) { toast(e.message); }
  };
  poll(id);
}

async function poll(id) {
  clearTimeout(pollTimer);
  let s;
  try { s = await api(`/api/searches/${id}`); }
  catch (e) { view.innerHTML = `<div class="empty" style="margin-top:60px">${esc(e.message)}. <a class="link" href="#/">New search</a></div>`; return; }
  if (location.hash !== `#/search/${id}`) return;
  state.search = s; state.results = s.results;
  drawProgress(s);
  const running = s.status === "queued" || s.status === "running";
  if (!running || s.results.length) drawResults();
  if (running) pollTimer = setTimeout(() => poll(id), 1000);
}

function drawProgress(s) {
  const running = s.status === "queued" || s.status === "running";
  $("#progressCard").classList.toggle("running", running);
  $("#stage").textContent = s.status === "failed" ? `Search failed: ${s.error || ""}` : s.stage;
  $("#bar").style.width = `${Math.round(s.progress * 100)}%`;
  $("#qmeta").innerHTML = `${esc(s.filename)}<br>${s.width}×${s.height} ${esc(s.format.toUpperCase())}<br>pHash ${esc(s.phash)}<br>${s.image_retained ? "original held temporarily (for retries)" : "original deleted ✓"}`;
  const ps = Object.entries(s.provider_status || {});
  $("#provStatus").innerHTML = ps.map(([k, v]) => `<li><span>${esc(v.label || k)}${v.error ? `<span class="err">${esc(v.error)}</span>` : ""}</span>
    <span>${v.state === "running" ? `<span class="spin"></span>` : v.state === "ok" ? `<span style="color:var(--ok)">${v.count} raw</span>` : `<span style="color:var(--bad)">failed</span>`}</span></li>`).join("")
    || (running ? "" : `<li class="muted">No providers ran. Configure one on the <a class="link" href="#/status">Status</a> page.</li>`);
  $("#retry").hidden = running || !(s.image_retained && (s.status === "failed" || ps.some(([, v]) => v.state === "failed")));
}

function readFilters() {
  const f = new FormData($("#filters"));
  return {
    mt: new Set(f.getAll("mt")), src: f.get("src"), pageonly: f.has("pageonly"), verified: f.has("verified"),
    from: f.get("from"), to: f.get("to"), domain: (f.get("domain") || "").trim().toLowerCase(), showHidden: f.has("showHidden"),
  };
}
function applyFilters(rs, f) {
  const terms = f.domain.split(/[,\s]+/).filter(Boolean);
  const inc = terms.filter(t => !t.startsWith("-")), exc = terms.filter(t => t.startsWith("-")).map(t => t.slice(1));
  return rs.filter(r => {
    if (!f.showHidden && r.hidden) return false;
    if (!f.mt.has(r.match_type)) return false;
    if (f.src === "news" && !r.is_news) return false;
    if (f.src === "web" && r.is_news) return false;
    if (f.pageonly && !r.page_url) return false;
    if (f.verified && !r.verified) return false;
    if (state.fmts.size && !state.fmts.has(r.image_format || "unknown")) return false;
    if ((f.from || f.to)) {
      if (!r.indexed_at) return false;
      const d = r.indexed_at.slice(0, 10);
      if (f.from && d < f.from) return false;
      if (f.to && d > f.to) return false;
    }
    if (inc.length && !inc.some(t => r.domain.includes(t))) return false;
    if (exc.some(t => r.domain.includes(t))) return false;
    return true;
  });
}

function drawResults() {
  const all = state.results, s = state.search;
  // format chips
  const fmts = [...new Set(all.map(r => r.image_format || "unknown"))].sort();
  $("#fmtChips").innerHTML = fmts.map(x => `<span class="chip ${state.fmts.has(x) ? "on" : ""}" data-f="${esc(x)}">${esc(x)}</span>`).join("") || `<span class="muted small">–</span>`;
  $$("#fmtChips .chip").forEach(c => c.onclick = () => { state.fmts.has(c.dataset.f) ? state.fmts.delete(c.dataset.f) : state.fmts.add(c.dataset.f); drawResults(); });
  ORDER.forEach(t => { const el = $(`[data-c="${t}"]`); if (el) el.textContent = all.filter(r => r.match_type === t && !r.hidden).length; });

  const f = readFilters();
  const rs = applyFilters(all, f);
  const domains = new Set(rs.map(r => r.domain)).size;
  $("#rcount").textContent = `${rs.length} result${rs.length === 1 ? "" : "s"}`;
  $("#rsub").textContent = `${domains} domain${domains === 1 ? "" : "s"} · ${all.filter(r => r.verified).length} re-checked by Mirror · ${all.filter(r => r.hidden).length} hidden`;

  if (!rs.length) {
    const running = s && (s.status === "running" || s.status === "queued");
    $("#results").innerHTML = running ? `<div class="grid">${"<div class='skeleton'></div>".repeat(6)}</div>`
      : `<div class="empty">${all.length ? "No results match these filters." : "The configured providers found no public pages with this image."}</div>`;
    return;
  }
  const g = $("#groupBy").value;
  let groups;
  if (g === "match") groups = ORDER.map(t => [LABEL[t], rs.filter(r => r.match_type === t)]);
  else if (g === "domain") { const m = {}; rs.forEach(r => (m[r.domain || "unknown"] ||= []).push(r)); groups = Object.entries(m).sort((a, b) => b[1].length - a[1].length); }
  else groups = [["", rs]];
  $("#results").innerHTML = groups.filter(([, x]) => x.length).map(([t, x]) =>
    `${t ? `<div class="group-title">${esc(t)} <span class="cnt">${x.length}</span></div>` : ""}<div class="grid">${x.map(card).join("")}</div>`).join("");
  bindCards();
}

function card(r) {
  const page = safeHref(r.page_url), img = safeHref(r.image_url), th = safeHref(r.thumbnail_url) || img;
  const sim = r.similarity != null ? `${Math.round(r.similarity * 100)}% similar` : null;
  return `<article class="card ${r.hidden ? "is-hidden" : ""}" data-id="${r.id}">
    <div class="thumb">${th ? `<img src="${esc(th)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span class="noimg">no image URL</span>`}
      <span class="badge ${r.match_type}">${LABEL[r.match_type]}</span>
      <span class="vbadge ${r.verified ? "v" : ""}" title="${r.verified ? "Mirror downloaded this image and compared perceptual hashes" : "Category comes from the provider. The image could not be downloaded to check it."}">${r.verified ? "✓ re-checked" : "provider-reported"}</span></div>
    <div class="cbody">
      <div class="domain">${esc(r.domain || "unknown domain")}</div>
      <div class="title">${esc(r.page_title || (page ? "Untitled page" : "Image only (no page reported)"))}</div>
      <div class="url" title="${esc(r.page_url || r.image_url)}">${esc(r.page_url || r.image_url || "")}</div>
      <div class="meta">
        ${r.providers.map(p => `<span class="tag">${esc(p.replace("_", " "))}</span>`).join("")}
        ${sim ? `<span class="tag">${sim}</span>` : ""}
        ${r.image_format ? `<span class="tag">${esc(r.image_format)}</span>` : ""}
        ${r.is_news ? `<span class="tag news">news</span>` : ""}
        ${r.indexed_at ? `<span class="tag">indexed ${esc(r.indexed_at.slice(0, 10))}</span>` : ""}
        ${r.broken_report ? `<span class="tag ${r.broken_report.confirmed_broken ? "broken" : ""}">${r.broken_report.confirmed_broken ? "broken link" : "link checked: OK"}</span>` : ""}
      </div>
    </div>
    <div class="actions">
      <a href="${esc(page || "#")}" target="_blank" rel="noopener noreferrer" ${page ? "" : "disabled"} title="Open webpage">Open</a>
      <a href="${esc(img || "#")}" target="_blank" rel="noopener noreferrer" ${img ? "" : "disabled"} title="View original image">Image</a>
      <button data-a="copy" title="Copy URL">Copy</button>
      <button data-a="save" class="${r.saved ? "on" : ""}" title="Save result">${r.saved ? "★" : "☆"}</button>
      <button data-a="hide" title="${r.hidden ? "Unhide" : "Hide result"}">${r.hidden ? "Unhide" : "Hide"}</button>
      <button data-a="report" title="Report broken link">Report</button>
    </div></article>`;
}

function bindCards() {
  $$(".card [data-a]").forEach(b => b.onclick = async () => {
    const el = b.closest(".card"), id = el.dataset.id, r = state.results.find(x => x.id === id);
    try {
      let upd;
      if (b.dataset.a === "copy") { await navigator.clipboard.writeText(r.page_url || r.image_url || ""); return toast("URL copied"); }
      if (b.dataset.a === "save") { upd = await api(`/api/results/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ saved: !r.saved }) }); toast(upd.saved ? "Saved" : "Removed from saved"); }
      if (b.dataset.a === "hide") { upd = await api(`/api/results/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ hidden: !r.hidden }) }); toast(upd.hidden ? "Result hidden" : "Result restored"); }
      if (b.dataset.a === "report") { b.innerHTML = `<span class="spin"></span>`; upd = await api(`/api/results/${id}/report`, { method: "POST" }); toast(upd.broken_report.confirmed_broken ? "Reported. The link is broken." : "Reported. The link still responds."); }
      Object.assign(r, upd);
      if (state.search) drawResults(); else el.outerHTML = card(r), bindCards();
    } catch (e) { toast(e.message); }
  });
}

// broken thumbnails (CSP forbids inline handlers, so this listens in the capture phase)
document.addEventListener("error", e => {
  const t = e.target;
  if (t.tagName === "IMG" && t.closest(".thumb")) t.replaceWith(Object.assign(document.createElement("span"), { className: "noimg", textContent: "thumbnail unavailable" }));
}, true);
document.addEventListener("click", e => { if (!e.target.closest(".menu")) $$(".menu-pop").forEach(m => m.hidden = true); });
getProviders().then(updateNavDot).catch(() => { });
route();
