/* Khinsider Sync - interface */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const api = async (path, opts = {}) => {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.detail || `HTTP ${r.status}`);
  return data;
};

function bytes(n) {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 2 : n < 100 ? 1 : 0)) + " " + u[i];
}
const num = (n) => (n || 0).toLocaleString();
const nFiles = (n) => `${num(n)} ${n === 1 ? "file" : "files"}`;
const nAlbums = (n) => `${num(n)} ${n === 1 ? "album" : "albums"}`;
const ago = (t) => {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + " min ago";
  if (s < 129600) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " days ago";
};
function duration(sec) {
  if (sec < 60) return "under a minute";
  const m = Math.round(sec / 60);
  if (m < 60) return m + " min";
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} h ${m % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
const clock = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const coll = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });
const FMT = (ext) => ext === "art" ? "Image" : ext.replace(/^\./, "").toUpperCase();

/* ---------------------------------------------------------------- library: platforms */
/* view: which albums the list shows. platform "" = all, "-" = albums without a platform */
let view = { platform: "", q: "", type: "", show: "all", sort: "name", dir: 1 };
try { view = Object.assign(view, JSON.parse(localStorage.getItem("view")) || {}, { q: "" }); } catch (e) {}
const saveView = () => { try { localStorage.setItem("view", JSON.stringify(view)); } catch (e) {} };
let platforms = [];                 // from /api/platforms
let noPlatform = 0;

async function loadPlatforms() {
  const d = await api("/api/platforms");
  platforms = d.platforms;
  noPlatform = d.no_platform;
  const sel = $("#f-type");
  const cur = view.type;
  sel.innerHTML = '<option value="">Every type</option>';
  for (const [t, n] of d.types) {
    const o = document.createElement("option");
    o.value = t === "Other" ? "" : t;
    if (t === "Other") continue;
    o.textContent = `${t} (${num(n)})`;
    sel.append(o);
  }
  sel.value = cur;
  renderPlatforms();
  return d;
}

function renderPlatforms() {
  const box = $("#plat-list");
  const f = $("#plat-filter").value.trim().toLowerCase();
  box.innerHTML = "";
  const rows = [{ slug: "", name: "All albums", albums: null, state: null }]
    .concat(platforms.filter((p) => !f || p.name.toLowerCase().includes(f)));
  if (noPlatform && !f) rows.push({ slug: "-", name: "No platform", albums: noPlatform, state: null });
  for (const p of rows) {
    const row = document.createElement("div");
    row.className = "prow" + (view.platform === p.slug ? " cur" : "") + (p.state && p.state !== "off" ? " sel" : "");
    row.innerHTML = `<input type="checkbox" class="tick"><button class="pname" type="button"></button><span class="pcount muted"></span>`;
    const tick = $(".tick", row);
    if (p.state === null) tick.style.visibility = "hidden";
    else {
      tick.checked = p.state === "on";
      tick.indeterminate = p.state === "partial";
      tick.dataset.state = p.state;
      tick.title = `Sync every ${p.name} album, including ones khinsider adds later`;
      tick.setAttribute("aria-label", `Sync every ${p.name} album`);
      tick.addEventListener("change", (e) => tickPlatform(p, e.target.dataset.state === "partial" ? true : e.target.checked));
    }
    $(".pname", row).textContent = p.name;
    $(".pname", row).addEventListener("click", () => { view.platform = p.slug; saveView(); renderPlatforms(); reload(); });
    $(".pcount", row).textContent = p.albums != null ? num(p.albums) : "";
    box.append(row);
  }
}
$("#plat-filter").addEventListener("input", renderPlatforms);

async function tickPlatform(p, include) {
  if (!include && p.state === "partial") include = true;
  try { await api("/api/select", { method: "POST", body: { key: "p:" + p.slug, include } }); }
  catch (e) { alert("Could not change the selection: " + e.message); }
  await Promise.all([loadPlatforms(), refreshList()]);
  poll(true);
}

/* ---------------------------------------------------------------- library: albums */
const alist = $("#alist");
const atpl = $("#album-tpl");
const PAGE = 200;
let loaded = 0, total = 0, listEpoch = 0;

function fillAlbum(el, a) {
  el._a = a;
  el.dataset.slug = a.slug;
  $(".n", el).textContent = a.name;
  const badge = $(".badge", el);
  badge.hidden = !a.synced;
  if (a.synced) {
    badge.textContent = a.downloaded ? "Downloaded" : "Partly downloaded";
    badge.className = "badge " + (a.downloaded ? "done" : "part");
    badge.title = a.downloaded
      ? `Already in your download folder${a.downloaded_at ? " (since " + new Date(a.downloaded_at * 1000).toLocaleDateString() + ")" : ""} - syncs leave it alone`
      : `${num(a.synced)} of ${nFiles(a.files)} downloaded - the next sync gets the rest`;
  }
  el.classList.toggle("done", !!a.downloaded);
  const sub = [a.platforms.join(", ") || "No platform", a.type, a.own_formats && a.own_formats.toUpperCase()].filter(Boolean).join(" · ");
  $(".sub", el).textContent = sub;
  el.title = a.title && a.title !== a.name ? a.title : a.name;
  $(".year", el).textContent = a.year || "";
  $(".size", el).textContent = a.read ? bytes(a.bytes) : a.missing ? "gone" : "";
  $(".size", el).title = a.read ? `${nFiles(a.files)} (${a.formats.map(FMT).join(", ")})` : "Not read yet: the size shows once its page was read (open it, or sync)";
  const tick = $(".tick", el);
  tick.checked = a.state === "on";
  tick.setAttribute("aria-label", `Sync ${a.name}`);
  tick.title = a.via ? "Ticked through its platform" : a.own === false ? "Unticked although its platform is ticked" : "Sync this album";
  el.classList.toggle("sel", a.state === "on");
  el.classList.toggle("via", !!a.via);
  el.classList.toggle("err", !!a.error);
  const meter = $(".c-meter", el);
  const pct = a.files ? a.synced / a.files : 0;
  meter.style.visibility = a.files ? "visible" : "hidden";
  $(".fill", meter).style.width = (pct * 100).toFixed(1) + "%";
  $(".pct", meter).textContent = a.files ? (a.synced === a.files ? "all" : a.synced ? Math.floor(pct * 100) + "%" : "") : "";
  meter.classList.toggle("has-failed", a.failed > 0);
  meter.title = a.files ? `${num(a.synced)} of ${nFiles(a.files)} synced (${bytes(a.synced_bytes)} of ${bytes(a.bytes)})`
    + (a.failed ? `\n${num(a.failed)} failed` : "") : "";
}

function makeAlbum(a) {
  const el = atpl.content.firstElementChild.cloneNode(true);
  fillAlbum(el, a);
  $(".tick", el).addEventListener("change", (e) => tickAlbum(el, e.target.checked));
  $(".aname", el).addEventListener("click", () => openAlbum(el.dataset.slug));
  $(".info", el).addEventListener("click", () => openAlbum(el.dataset.slug));
  return el;
}

function query(offset) {
  const p = new URLSearchParams({ platform: view.platform, q: view.q, type: view.type, show: view.show,
    sort: view.sort, dir: view.dir, offset, limit: PAGE });
  return api("/api/albums?" + p);
}

async function reload() {
  const epoch = ++listEpoch;
  showHead();
  let d;
  try { d = await query(0); } catch (e) { $("#list-count").textContent = "Could not load: " + e.message; return; }
  if (epoch !== listEpoch) return;
  alist.innerHTML = "";
  total = d.total;
  loaded = d.albums.length;
  for (const a of d.albums) alist.append(makeAlbum(a));
  if (!total) {
    alist.innerHTML = `<div class="loading">${view.q ? "No album matches “" + view.q.replace(/[<&]/g, "") + "”." : "No albums here."}</div>`;
  }
  $("#btn-more").hidden = loaded >= total;
  $("#list-count").textContent = nAlbums(total);
}

async function more() {
  const epoch = listEpoch;
  const d = await query(loaded);
  if (epoch !== listEpoch) return;
  for (const a of d.albums) alist.append(makeAlbum(a));
  loaded += d.albums.length;
  total = d.total;
  $("#btn-more").hidden = loaded >= total;
}
$("#btn-more").addEventListener("click", more);

/* the rows on screen get fresh numbers without losing the scroll position */
async function refreshList() {
  if (!loaded) return reload();
  const epoch = listEpoch;
  const p = new URLSearchParams({ platform: view.platform, q: view.q, type: view.type, show: view.show,
    sort: view.sort, dir: view.dir, offset: 0, limit: Math.min(1000, Math.max(PAGE, loaded)) });
  let d;
  try { d = await api("/api/albums?" + p); } catch (e) { return; }
  if (epoch !== listEpoch || selPending) return;
  const els = new Map([...alist.children].filter((c) => c._a).map((c) => [c.dataset.slug, c]));
  const same = d.albums.length === els.size && d.albums.every((a) => els.has(a.slug));
  if (!same) return reload();
  for (const a of d.albums) fillAlbum(els.get(a.slug), a);
  total = d.total;
  $("#list-count").textContent = nAlbums(total);
}

function showHead() {
  const p = platforms.find((x) => x.slug === view.platform);
  $("#list-title").textContent = view.platform === "" ? "All albums" : view.platform === "-" ? "Albums without a platform" : p ? p.name : view.platform;
  const wrap = $("#plat-tick-wrap");
  wrap.hidden = !p;
  if (p) {
    $("#plat-tick-name").textContent = p.name;
    const t = $("#plat-tick");
    t.checked = p.state === "on";
    t.indeterminate = p.state === "partial";
    t.onchange = (e) => tickPlatform(p, e.target.checked);
  }
  for (const b of document.querySelectorAll(".acols .sort")) {
    const on = b.dataset.sort === view.sort;
    b.classList.toggle("asc", on && view.dir === 1);
    b.classList.toggle("desc", on && view.dir === -1);
    b.setAttribute("aria-sort", on ? (view.dir === 1 ? "ascending" : "descending") : "none");
  }
}
for (const b of document.querySelectorAll(".acols .sort")) {
  b.addEventListener("click", () => {
    const k = b.dataset.sort;
    if (view.sort !== k) view = { ...view, sort: k, dir: k === "name" ? 1 : -1 };
    else view.dir = -view.dir;
    saveView();
    reload();
  });
}

/* ticks are saved one after another in click order; one refresh follows the last click */
let selQueue = Promise.resolve();
let selPending = 0;
let selTimer = null;
function tickAlbum(el, include) {
  el.classList.toggle("sel", include);
  selPending++;
  clearTimeout(selTimer);
  selQueue = selQueue.then(async () => {
    try { await api("/api/select", { method: "POST", body: { key: "a:" + el.dataset.slug, include } }); }
    catch (e) { alert("Could not change the selection: " + e.message); }
    selPending--;
    if (!selPending) selTimer = setTimeout(async () => { await Promise.all([refreshList(), loadPlatforms()]); poll(true); }, 150);
  });
  return selQueue;
}

let searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    view.q = $("#search").value.trim();
    if (view.q && view.platform) { view.platform = ""; saveView(); renderPlatforms(); }   // search everything
    reload();
  }, 250);
});
$("#f-type").addEventListener("change", (e) => { view.type = e.target.value; saveView(); reload(); });
$("#f-show").addEventListener("change", (e) => { view.show = e.target.value; saveView(); reload(); });

/* ---------------------------------------------------------------- album dialog */
let current = null;                // slug in the dialog
async function openAlbum(slug, readIfNeeded = true) {
  const dlg = $("#album-dlg");
  current = slug;
  $("#al-body").innerHTML = "";
  $("#al-sub").textContent = "Loading…";
  if (!dlg.open) dlg.showModal();
  let d;
  try { d = await api("/api/album?slug=" + encodeURIComponent(slug)); }
  catch (e) { $("#al-sub").textContent = "Could not load the album: " + e.message; return; }
  if (current !== slug) return;
  showAlbum(d);
  if (readIfNeeded && !d.album.read && !d.album.missing) readAlbum(slug);
}
function showAlbum(d) {
  const a = d.album;
  $("#al-title").textContent = a.name;
  const bits = [a.platforms.join(", ") || "No platform", a.type, a.year, a.added && `added ${a.added}`,
    a.formats.length && a.formats.map(FMT).join(" + ")].filter(Boolean);
  $("#al-meta").textContent = bits.join(" · ") + (a.title && a.title !== a.name ? `\n${a.title}` : "");
  const tick = $("#al-tick");
  tick.checked = a.state === "on";
  $("#al-tick-text").textContent = a.via ? "Sync this album (ticked through its platform)" : "Sync this album";
  tick.onchange = async (e) => {
    try { await api("/api/select", { method: "POST", body: { key: "a:" + a.slug, include: e.target.checked } }); }
    catch (err) { alert("Could not change the selection: " + err.message); }
    openAlbum(a.slug, false);
    refreshList(); loadPlatforms(); poll(true);
  };
  $("#al-site").href = d.page;
  $("#al-check").hidden = !a.synced;
  const fsel = $("#al-formats");
  const names = { mp3: "MP3", flac: "FLAC where offered", best: "best quality", all: "every format" };
  const describe = (v) => {
    if (names[v]) return names[v];
    const m = /^(first|all):(.*)$/.exec(v || "") || [null, "all", v || ""];
    const list = m[2].split(",").filter(Boolean).map((x) => (x === "art" ? "images" : x.replace(/^\./, "").toUpperCase()));
    return (m[1] === "first" ? "first of " : "") + list.join(", ");
  };
  fsel.options[0].textContent = `Same as Settings (${describe(d.default_formats)})`;
  if (d.formats && ![...fsel.options].some((o) => o.value === d.formats)) {
    const o = document.createElement("option"); o.value = d.formats; o.textContent = describe(d.formats); fsel.append(o);
  }
  fsel.value = d.formats || "";
  fsel.onchange = async () => {
    try { await api("/api/album-formats", { method: "POST", body: { slug: a.slug, formats: fsel.value } }); }
    catch (err) { alert("Not saved: " + err.message); }
    openAlbum(a.slug, false); refreshList(); poll(true);
  };
  const place = $("#al-place");
  place.value = d.dir;
  place.dataset.orig = d.dir;
  place.classList.toggle("renamed", !!d.place);
  const sub = $("#al-sub");
  if (a.missing) sub.textContent = "khinsider no longer has this album.";
  else if (!a.read) sub.textContent = a.error ? `Its page couldn't be read: ${a.error}` : "Reading the album's page…";
  else if (a.downloaded) sub.textContent = `Downloaded${a.downloaded_at ? " on " + new Date(a.downloaded_at * 1000).toLocaleDateString() : ""}: `
    + `all ${nFiles(a.files)} (${bytes(a.bytes)}) are in your download folder. Syncs leave it alone; "Check files" looks for any you deleted.`
    + ` Greyed rows aren't synced (format or image setting).`;
  else sub.textContent = `A sync saves ${nFiles(a.files)} of this album (${bytes(a.bytes)}); ${num(a.synced)} ${a.synced === 1 ? "is" : "are"} in the folder`
    + (a.failed ? `, ${num(a.failed)} failed` : "") + `. Page read ${ago(a.read)}. Greyed rows aren't synced (format or image setting).`;
  const body = $("#al-body");
  body.innerHTML = "";
  for (const f of d.files) {
    const tr = document.createElement("tr");
    if (!f.wanted) tr.className = "unwanted";
    const status = f.local ? (f.local === "already in folder" ? "was in folder" : "downloaded")
      : f.error ? "failed" : f.wanted ? "not synced" : "-";
    const n = f.kind === "art" ? "" : (f.disc ? f.disc + "-" : "") + (f.num || "");
    for (const v of [n, f.kind === "art" ? f.name : f.title || f.name, FMT(f.kind === "art" ? "art" : f.ext), f.size || ""]) {
      const td = document.createElement("td"); td.textContent = v; tr.append(td);
    }
    tr.children[1].title = f.rel;
    const td = document.createElement("td");
    td.textContent = status;
    td.className = f.local ? "st-ok" : f.error ? "st-bad" : "st-none";
    if (f.error) td.title = f.error;
    tr.append(td);
    body.append(tr);
  }
}
async function readAlbum(slug) {
  const btn = $("#al-read");
  btn.disabled = true;
  $("#al-sub").textContent = "Reading the album's page…";
  try {
    const r = await api("/api/album/read", { method: "POST", body: { slug } });
    if (current === slug) await openAlbum(r.slug || slug, false);
    refreshList();
    poll(true);
  } catch (e) { $("#al-sub").textContent = "Could not read the album's page: " + e.message; }
  btn.disabled = false;
}
$("#al-read").addEventListener("click", () => current && readAlbum(current));
$("#al-check").addEventListener("click", async () => {
  if (!current) return;
  const slug = current;
  try {
    const r = await api("/api/album/check", { method: "POST", body: { slug } });
    await openAlbum(slug, false);
    $("#al-sub").textContent = r.missing.length
      ? `${nFiles(r.missing.length)} missing from the download folder - the next sync downloads ${r.missing.length === 1 ? "it" : "them"} again.`
      : "Every file is still in the download folder.";
    refreshList(); poll(true);
  } catch (e) { alert("Could not check the files: " + e.message); }
});
$("#al-close").addEventListener("click", () => $("#album-dlg").close());
async function savePlace() {
  const input = $("#al-place");
  try {
    await api("/api/album-place", { method: "POST", body: { slug: current, place: input.value.trim() } });
    openAlbum(current, false);
    poll(true);
  } catch (e) { alert("Not saved: " + e.message); input.value = input.dataset.orig; }
}
$("#al-place-save").addEventListener("click", savePlace);
$("#al-place").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); savePlace(); }
  else if (e.key === "Escape") { e.preventDefault(); e.target.value = e.target.dataset.orig; }
});

/* ---------------------------------------------------------------- status / activity */
let lastLog = 0;
let pollTimer;
let wasBusy = false;
let lastListRefresh = 0;

function setStats(el, pairs) {
  el.hidden = false;
  el.innerHTML = "";
  for (const [label, value, cls, kind] of pairs) {
    // counters with a kind open the list of files behind them
    const d = document.createElement(kind ? "button" : "div");
    d.innerHTML = `<span class="muted"></span> <b></b>`;
    d.firstChild.textContent = label;
    d.lastChild.textContent = value;
    if (cls) d.lastChild.className = cls;
    if (kind) {
      d.type = "button";
      d.className = "stat-link";
      d.title = `Show the files: ${label.toLowerCase()}`;
      d.addEventListener("click", () => showSyncFiles(kind, label));
    }
    el.append(d);
  }
}

function renderStatus(st) {
  const ver = $("#version");
  if (st.version && ver.dataset.v !== st.version) {
    ver.dataset.v = st.version;
    ver.textContent = "v" + st.version;
    ver.href = `https://github.com/sireteanu/khinsider-sync/releases/tag/v${st.version}`;
  }
  const busy = st.busy;
  const job = st.job;
  const pill = $("#jobpill");
  const syncing = st.sync && job && job.phase === "downloading" && busy && !st.sync.outage;
  const outage = busy && st.sync && st.sync.outage;
  pill.className = "pill " + (outage ? "warn" : busy ? "busy" : st.sync && st.sync.paused ? "warn" : "idle");
  pill.textContent = outage ? "Waiting for site" : busy ? ({ scan: "Scanning", read: "Reading" }[job.kind] || "Syncing") :
    (st.sync && st.sync.paused ? "Paused" : "Idle");
  renderOutage(outage);
  const sel = st.selection;
  $("#btn-scan").disabled = busy;
  $("#btn-read").disabled = busy || !sel.albums;
  $("#btn-sync").disabled = busy || !sel.albums;
  $("#btn-sync").title = sel.albums ? "Download new, incomplete and changed files of the ticked albums"
    : "Tick the albums or platforms you want first";
  $("#btn-stop").disabled = !busy;

  // storage strip
  const todo = Math.max(0, sel.bytes - sel.synced_bytes);
  $("#sel-summary").textContent = sel.albums
    ? `${bytes(sel.bytes)} ticked in ${nAlbums(sel.albums)}` : "Nothing ticked yet";
  const cat = st.catalog;
  $("#cat-summary").textContent = cat.albums
    ? `khinsider: ${num(cat.albums)} albums on ${num(cat.platforms)} platforms. Last scan ${ago(cat.last_full_scan && cat.last_full_scan.finished)}.`
    : "";
  const tot = sel.bytes || 1;
  $("#bar-synced").style.width = (100 * sel.synced_bytes / tot) + "%";
  $("#bar-todo").style.width = (100 * todo / tot) + "%";
  $("#lg-synced").textContent = `Synced ${bytes(sel.synced_bytes)} (${nFiles(sel.synced)})`;
  $("#lg-todo").textContent = `To download ${bytes(todo)} (${nFiles(Math.max(0, sel.files - sel.synced))})`;
  $("#lg-done").textContent = sel.albums ? `${num(sel.downloaded)} of ${nAlbums(sel.albums)} downloaded` : "";
  $("#lg-unread").textContent = sel.unread ? `${nAlbums(sel.unread)} not read yet: their size counts once the next sync reads them` : "";
  $("#lg-dest").textContent = `Saving to ${st.data_dir}`;
  $("#empty").hidden = !!cat.albums || busy;

  // job
  const line = $("#job-line");
  if (job) {
    const what = { scan: "Scan", read: "Reading album pages", sync: "Sync", "scan+sync": "Scan and sync" }[job.kind] || job.kind;
    line.textContent = busy
      ? `${what}: ${outage ? "waiting for khinsider" : job.phase} - started ${clock(job.started)}`
      : `Last ${what.toLowerCase()} ${job.phase === "done" ? "finished" : job.phase} at ${clock(job.finished || job.started)}`
        + (job.message ? `: ${job.message}` : "");
  } else line.textContent = "No job running.";

  const sc = $("#scan-stats");
  if (st.scan && busy) {
    const s = st.scan;
    const pairs = s.what === "site"
      ? [["Pages read", `${num(s.pages)}${s.total ? " of " + num(s.total + 1) : ""}`], ["Albums found", num(s.albums)],
         ["New albums", num(s.new)], ["Failed pages", num(s.errors), s.errors ? "bad" : ""]]
      : [["Album pages", `${num(s.read)} of ${num(s.total)}`], ["Files found", num(s.files)],
         ["Failed", num(s.errors), s.errors ? "bad" : ""], ["Waiting", num(s.queued)]];
    pairs.push(["Speed", `${s.rate} pages/s`], ["Parallel", `${s.parallel}, ${s.delay}s apart`]);
    setStats(sc, pairs);
  } else sc.hidden = true;

  const sy = $("#sync-stats");
  renderDownloads(st.sync, syncing);
  if (st.sync) {
    const s = st.sync;
    const uptodate = (s["up to date"] || 0) + (s["already in folder"] || 0);
    setStats(sy, [["New", num(s.new), "", "new"], ["Updated", num(s.updated), s.updated ? "warn" : "", "updated"],
      ["Resumed", num(s.resumed), "", "resumed"], ["Up to date", num(uptodate), "", "uptodate"],
      ["Failed", num(s.failed), s.failed ? "bad" : "", "failed"], ["Downloaded", bytes(s.bytes)],
      ["Albums already done", num(s.albums_done || 0)],
      ["Speed", syncing ? bytes(s.speed) + "/s" : "-"],
      ["Parallel", syncing ? `${s.running} (${s.gate})` : "-"]]);
  } else sy.hidden = true;
  const paused = $("#sync-paused");
  paused.hidden = !(st.sync && st.sync.paused);
  if (!paused.hidden) paused.textContent = "Paused: " + st.sync.paused;
  renderEta(st, busy);
  renderLeftovers(st.leftovers, busy);
  $("#next-run").textContent = st.next_run ? `Next automatic sync at ${new Date(st.next_run * 1000).toLocaleString()}` : "";

  // log
  const log = $("#log");
  for (const l of st.log) {
    const li = document.createElement("li");
    li.className = l.level;
    li.innerHTML = "<time></time><span></span>";
    li.firstChild.textContent = clock(l.t);
    li.lastChild.textContent = l.msg;
    log.append(li);
    lastLog = l.id;
  }
  while (log.children.length > 300) log.firstChild.remove();
  if (st.log.length) log.scrollTop = log.scrollHeight;

  // keep the list's numbers fresh while something is happening, and once when it ends
  const now = Date.now();
  if ((busy && now - lastListRefresh > 5000) || (wasBusy && !busy)) {
    lastListRefresh = now;
    refreshList();
    if (wasBusy && !busy) loadPlatforms();
  }
  wasBusy = busy;
}

/* files being downloaded right now: one row each, kept between polls so bars move smoothly */
const dlRows = new Map();      // rel -> {el, have, t, speed}
function renderDownloads(s, syncing) {
  const card = $("#dl-card"), box = $("#downloads");
  const active = (s && s.active) || [];
  card.hidden = !syncing && !active.length;
  if (card.hidden) { box.innerHTML = ""; dlRows.clear(); return; }
  $("#dl-count").textContent = active.length ? `${active.length} ${active.length === 1 ? "file" : "files"}` : "";
  const now = Date.now() / 1000, seen = new Set();
  for (const a of active) {
    seen.add(a.rel);
    let r = dlRows.get(a.rel);
    if (!r) {
      const el = document.createElement("div");
      el.className = "dl";
      el.innerHTML = `<div class="dl-top"><span class="dl-name"></span><span class="dl-pct"></span></div>
        <div class="dl-dir"></div><span class="meter"><span class="fill"></span></span><div class="dl-size"></div>`;
      const slash = a.rel.lastIndexOf("/");
      $(".dl-name", el).textContent = a.rel.slice(slash + 1);
      $(".dl-dir", el).textContent = slash > 0 ? a.rel.slice(0, slash) : "";
      el.title = a.rel;
      r = { el, have: a.have, t: now, speed: 0 };
      dlRows.set(a.rel, r);
    } else if (now > r.t) {
      const inst = Math.max(0, (a.have - r.have) / (now - r.t));
      r.speed = r.speed ? r.speed * 0.6 + inst * 0.4 : inst;       // smoothed per-file speed
      r.have = a.have; r.t = now;
    }
    box.append(r.el);                                               // keeps the server's order
    const pct = a.total ? Math.min(100, 100 * a.have / a.total) : 0;
    $(".fill", r.el).style.width = pct.toFixed(1) + "%";
    $(".dl-pct", r.el).textContent = a.total ? Math.floor(pct) + "%" : "";
    let size = a.total ? `${bytes(a.have)} of ${bytes(a.total)}` : bytes(a.have);
    if (r.speed > 0) {
      size += ` - ${bytes(r.speed)}/s`;
      if (a.total && a.total > a.have) size += `, ${duration((a.total - a.have) / r.speed)} left`;
    }
    $(".dl-size", r.el).textContent = size;
  }
  for (const [rel, r] of dlRows) if (!seen.has(rel)) { r.el.remove(); dlRows.delete(rel); }
  if (!active.length) {
    if (!box.querySelector(".dl-wait")) box.innerHTML = `<p class="muted small dl-wait">Waiting for the next file…</p>`;
  } else { const w = box.querySelector(".dl-wait"); if (w) w.remove(); }
}

function renderOutage(o) {
  const box = $("#outage");
  box.hidden = !o;
  if (!o) return;
  const now = Date.now() / 1000;
  const next = Math.max(0, o.next_check - now);
  $("#outage-text").textContent =
    `Down since ${clock(o.since)} (${o.error}). Downloads are on hold and carry on by themselves when it's ` +
    `back; nothing is marked as failed. Next check ${next < 5 ? "now" : "in " + duration(next).replace("under a minute", "less than a minute")}. ` +
    `If it's still down at ${new Date(o.gives_up * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}, ` +
    `the sync pauses and the next one picks up from there.`;
}

function renderEta(st, busy) {
  const box = $("#eta-box");
  const s = st.sync;
  if (!s || !s.to_fetch) { box.hidden = true; return; }
  box.hidden = false;
  const total = s.bytes_planned || 0;
  const left = Math.min(s.bytes_left || 0, total);
  $("#eta-total").textContent = `${bytes(total)} in ${num(s.to_fetch)} ${s.to_fetch === 1 ? "file" : "files"}`;
  const downloading = busy && st.job && st.job.phase === "downloading";
  let time;
  if (downloading && s.paused) time = "Paused";
  else if (downloading && s.outage) time = "Waiting for site";
  else if (downloading) time = s.eta != null ? duration(s.eta) : "Estimating…";
  else time = s.files_left ? "Stopped" : "Finished";
  $("#eta-time").textContent = time;
  $("#eta-bar").style.width = (total ? 100 * (total - left) / total : 100) + "%";
  let sub = `${bytes(total - left)} of ${bytes(total)} done, ${num(s.files_left)} ${s.files_left === 1 ? "file" : "files"} left`;
  if (downloading && s.eta != null && !s.paused) sub += ` - done around ${clock(Date.now() / 1000 + s.eta)}`;
  $("#eta-sub").textContent = sub;
}


async function poll(once = false) {
  clearTimeout(pollTimer);
  let busy = false;
  try {
    const st = await api(`/api/status?log_since=${lastLog}`);
    busy = st.busy;
    renderStatus(st);
  } catch (e) {
    $("#job-line").textContent = "Lost contact with Khinsider Sync - is it still running?";
  }
  // a hidden tab stops asking (keeps the server and its disks idle); coming back refreshes at once
  if (!document.hidden) pollTimer = setTimeout(poll, busy ? 1000 : 4000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

/* ---------------------------------------------------------------- buttons */
async function startJob(path, body) {
  try { await api(path, { method: "POST", body }); }
  catch (e) { alert(e.message === "already busy" ? "Something is already running - stop it first." : e.message); }
  poll(true);
}
$("#btn-scan").addEventListener("click", () => startJob("/api/scan"));
$("#btn-scan-empty").addEventListener("click", () => startJob("/api/scan"));
$("#btn-sync").addEventListener("click", () => startJob("/api/sync"));
$("#btn-stop").addEventListener("click", () => startJob("/api/stop"));
$("#btn-read").addEventListener("click", () => startJob("/api/read", {}));
$("#btn-clear-sel").addEventListener("click", async () => {
  if (!confirm("Untick every album and platform? Files already downloaded stay where they are.")) return;
  await api("/api/selection/clear", { method: "POST" });
  await Promise.all([loadPlatforms(), refreshList()]);
  poll(true);
});

/* ---------------------------------------------------------------- settings */
const form = $("#settings-form");
function syncFields() {
  $("#par-label").textContent = form.parallel_mode.value === "auto" ? "Upper limit" : "Number of downloads";
}
form.parallel_mode.addEventListener("change", syncFields);

/* formats: an ordered list of formats (and "art" = the album's images), each on or off, and whether
   an album gets only the first ticked format it has or every one. Saved as "first:.flac,.mp3,art". */
const FMT_ALL = [".flac", ".wav", ".ape", ".wv", ".m4a", ".opus", ".ogg", ".aac", ".mp3", ".wma", "art"];
const FMT_NAME = { art: "Images (covers, scans)" };
const BEST = [".flac", ".wav", ".ape", ".wv", ".m4a", ".opus", ".ogg", ".aac", ".mp3"];
let fmt = { mode: "first", items: [] }, fmtDirty = false;
function fmtFrom(setting, art) {
  const s = String(setting || "mp3").trim().toLowerCase();
  let mode = "first", on = [];
  if (s === "mp3") on = [".mp3"];
  else if (s === "flac") on = [".flac", ".mp3"];
  else if (s === "best") on = BEST.slice();
  else if (s === "all") { mode = "all"; on = FMT_ALL.filter((f) => f !== "art"); }
  else {
    let list = s;
    if (s.includes(":")) { [mode, list] = s.split(/:(.*)/s); }
    else mode = "all";
    on = list.split(",").map((x) => x.trim()).filter(Boolean)
      .map((x) => (["art", "images", "image"].includes(x) ? "art" : "." + x.replace(/^\./, "")));
  }
  if (!s.includes(":") && art && !on.includes("art")) on.push("art");
  const rest = FMT_ALL.filter((f) => !on.includes(f));
  return { mode, items: on.map((k) => ({ key: k, on: true })).concat(rest.map((k) => ({ key: k, on: false }))) };
}
function fmtString() {
  return fmt.mode + ":" + fmt.items.filter((i) => i.on).map((i) => i.key).join(",");
}
function renderFmt() {
  $("#fmt-mode").value = fmt.mode;
  const ol = $("#fmt-list");
  ol.innerHTML = "";
  fmt.items.forEach((it, i) => {
    const li = document.createElement("li");
    li.className = it.on ? "on" : "";
    li.innerHTML = `<label class="check"><input type="checkbox"><span></span></label>
      <span class="fmt-move"><button type="button" class="btn icon small up" aria-label="Move up">▲</button>
      <button type="button" class="btn icon small down" aria-label="Move down">▼</button></span>`;
    const name = FMT_NAME[it.key] || it.key.slice(1).toUpperCase();
    $("span", li).textContent = name;
    const box = $("input", li);
    box.checked = it.on;
    box.addEventListener("change", () => { it.on = box.checked; fmtDirty = true; renderFmt(); });
    $(".up", li).disabled = i === 0;
    $(".down", li).disabled = i === fmt.items.length - 1;
    $(".up", li).setAttribute("aria-label", `Move ${name} up`);
    $(".down", li).setAttribute("aria-label", `Move ${name} down`);
    $(".up", li).addEventListener("click", () => moveFmt(i, -1));
    $(".down", li).addEventListener("click", () => moveFmt(i, 1));
    ol.append(li);
  });
}
function moveFmt(i, d) {
  const j = i + d;
  if (j < 0 || j >= fmt.items.length) return;
  [fmt.items[i], fmt.items[j]] = [fmt.items[j], fmt.items[i]];
  fmtDirty = true;
  renderFmt();
  const btn = $("#fmt-list").children[j].querySelector(d < 0 ? ".up" : ".down");
  (btn.disabled ? $("#fmt-list").children[j].querySelector("input") : btn).focus();
}
$("#fmt-mode").addEventListener("change", (e) => { fmt.mode = e.target.value; fmtDirty = true; });
for (const b of document.querySelectorAll(".fmt-presets button")) {
  b.addEventListener("click", () => {
    const art = fmt.items.some((i) => i.key === "art" && i.on);
    const p = b.dataset.preset;
    fmt = fmtFrom(p === "flacmp3" ? "all:.flac,.mp3" : p === "flac" ? "first:.flac,.mp3" : p === "mp3" ? "first:.mp3" : p, false);
    if (art) {                          // keep the images choice
      const a = fmt.items.find((i) => i.key === "art");
      a.on = true;
      fmt.items = fmt.items.filter((i) => i !== a);
      fmt.items.splice(fmt.items.filter((i) => i.on).length, 0, a);
    }
    fmtDirty = true;
    renderFmt();
  });
}

$("#btn-settings").addEventListener("click", async () => {
  const [{ settings: s, data_locked }, plats] = await Promise.all([api("/api/settings"), api("/api/platforms")]);
  form.data_dir.value = s.data_dir;
  form.data_dir.readOnly = data_locked;
  $("#data-hint").textContent = data_locked
    ? "Set by the container's /data mapping - change it in the Docker template."
    : "Each album gets a folder of its own in here.";
  fmt = fmtFrom(s.formats, !!s.album_art);
  fmtDirty = false;
  renderFmt();
  form.folder_layout.value = s.folder_layout || "album";
  form.art_folder.checked = !!s.art_folder;
  form.track_names.value = s.track_names || "site";
  form.impersonate.checked = s.impersonate !== false;
  const box = $("#types-list");
  box.innerHTML = "";
  const kinds = plats.types.map(([t]) => t).filter((t) => t !== "Other");
  for (const t of new Set([...kinds, ...(s.platform_types || [])])) {
    const l = document.createElement("label");
    l.className = "check";
    l.innerHTML = `<input type="checkbox" value=""><span></span>`;
    $("input", l).value = t;
    $("input", l).checked = (s.platform_types || []).includes(t);
    $("span", l).textContent = t;
    box.append(l);
  }
  if (!box.children.length) box.innerHTML = '<span class="muted small">Scan the site first.</span>';
  const auto = String(s.parallel) === "auto";
  form.parallel_mode.value = auto ? "auto" : "fixed";
  form.parallel_n.value = auto ? s.parallel_max : s.parallel;
  form.max_rate.value = s.max_rate || "";
  form.min_free.value = s.min_free || "";
  form.file_mode.value = s.file_mode || "";
  form.sync_every_hours.value = s.sync_every_hours;
  form.full_scan_every_days.value = s.full_scan_every_days;
  form.outage_wait_hours.value = s.outage_wait_hours ?? 12;
  form.resume_interrupted.checked = s.resume_interrupted !== false;
  $("#settings-error").hidden = true;
  syncFields();
  $("#settings-dlg").showModal();
});

$("#settings-save").addEventListener("click", async (e) => {
  e.preventDefault();
  const auto = form.parallel_mode.value === "auto";
  const body = {
    folder_layout: form.folder_layout.value,
    art_folder: form.art_folder.checked,
    track_names: form.track_names.value,
    impersonate: form.impersonate.checked,
    platform_types: [...document.querySelectorAll("#types-list input:checked")].map((i) => i.value),
    parallel: auto ? "auto" : form.parallel_n.value,
    max_rate: form.max_rate.value.trim(),
    min_free: form.min_free.value.trim() || "0",
    file_mode: form.file_mode.value.trim(),
    sync_every_hours: form.sync_every_hours.value || 0,
    full_scan_every_days: form.full_scan_every_days.value || 0,
    outage_wait_hours: form.outage_wait_hours.value || 0,
    resume_interrupted: form.resume_interrupted.checked,
  };
  if (auto) body.parallel_max = form.parallel_n.value || 8;
  if (fmtDirty) body.formats = fmtString();      // untouched: keep the setting as it is
  if (!form.data_dir.readOnly) body.data_dir = form.data_dir.value.trim();
  try {
    await api("/api/settings", { method: "POST", body });
    $("#settings-dlg").close();
    await Promise.all([loadPlatforms(), refreshList()]);
    poll(true);
  } catch (err) {
    const p = $("#settings-error");
    p.textContent = "Not saved: " + err.message;
    p.hidden = false;
  }
});

/* ---------------------------------------------------------------- restart */
/* power menu: Restart / Shut down */
const powerBtn = $("#btn-menu"), powerMenu = $("#app-menu");
function powerOpen(open) {
  powerMenu.hidden = !open;
  powerBtn.setAttribute("aria-expanded", String(open));
  if (open) powerMenu.querySelector("button").focus();
}
powerBtn.addEventListener("click", (e) => { e.stopPropagation(); powerOpen(powerMenu.hidden); });
document.addEventListener("click", (e) => { if (!powerMenu.hidden && !powerMenu.contains(e.target)) powerOpen(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !powerMenu.hidden) { powerOpen(false); powerBtn.focus(); } });
for (const b of powerMenu.querySelectorAll("button")) b.addEventListener("click", () => powerOpen(false));

$("#btn-shutdown").addEventListener("click", async () => {
  powerOpen(false);
  if (!confirm("Shut down Khinsider Sync? A sync that is running continues the next time you start it.")) return;
  let docker = false;
  try { docker = !!(await api("/api/shutdown", { method: "POST" })).docker; }
  catch (e) { alert("Could not shut down: " + e.message); return; }
  clearTimeout(pollTimer);
  $("#shutdown-how").textContent = docker
    ? "Start it again in Unraid: Docker tab, khinsider-sync, Start (or Compose Up on its stack)."
    : "Start it again with start.bat.";
  $("#shutdown").hidden = false;
});

$("#btn-restart").addEventListener("click", async () => {
  powerOpen(false);
  if (!confirm("Restart Khinsider Sync? A sync that is running carries on by itself afterwards.")) return;
  try { await api("/api/restart", { method: "POST" }); }
  catch (e) { alert("Could not restart: " + e.message); return; }
  $("#restarting").hidden = false;
  clearTimeout(pollTimer);
  const t0 = Date.now();
  await new Promise((r) => setTimeout(r, 1500));
  while (Date.now() - t0 < 120000) {
    try { const r = await fetch("/api/health", { cache: "no-store" }); if (r.ok) { location.reload(); return; } } catch (e) {}
    await new Promise((r) => setTimeout(r, 1000));
  }
  $("#restarting").querySelector("p").textContent = "It hasn't come back after 2 minutes - check the window or container it runs in.";
});

/* ---------------------------------------------------------------- what changed */
const KIND = {
  new: ["New", "ok"], updated: ["Updated", "warn"], resumed: ["Resumed", ""], uptodate: ["Up to date", ""],
  failed: ["Failed", "bad"], deleted: ["Deleted", "bad"],
};
const KIND_ORDER = ["new", "updated", "resumed", "deleted", "failed"];
const dayName = (d) => new Date(d + "T12:00:00").toLocaleDateString([], { weekday: "long", year: "numeric", month: "long", day: "numeric" });
function fileRow(f, third) {
  const tr = document.createElement("tr");
  tr.innerHTML = "<td class=\"p\"></td><td class=\"n\"></td><td></td>";
  tr.children[0].textContent = f.path;
  tr.children[0].title = f.path;
  tr.children[1].textContent = f.size ? bytes(f.size) : "-";
  tr.children[2].textContent = third;
  tr.children[2].title = third;
  return tr;
}
async function showSyncFiles(kind, label) {
  const dlg = $("#list-dlg");
  $("#list-title").textContent = `${label} in this sync`;
  $("#list-sub").textContent = "Loading…";
  $("#list-col3").textContent = kind === "failed" ? "Why" : "Time";
  const body = $("#list-body");
  body.innerHTML = "";
  if (!dlg.open) dlg.showModal();
  try {
    const d = await api(`/api/sync/files?kind=${kind}`);
    $("#list-sub").textContent = d.total
      ? `${nFiles(d.total)}, ${bytes(d.files.reduce((a, f) => a + (f.size || 0), 0))}` + (d.total > d.files.length ? ` (first ${num(d.files.length)} shown)` : "")
      : "None in this sync.";
    d.files.sort((x, y) => coll.compare(x.path, y.path));
    for (const f of d.files) body.append(fileRow(f, kind === "failed" ? (f.note || "") : f.note || clock(f.t)));
  } catch (e) { $("#list-sub").textContent = "Could not load the list: " + e.message; }
}
async function openChanges() {
  const dlg = $("#changes-dlg"), box = $("#changes-days");
  box.innerHTML = "<p class=\"muted\">Loading…</p>";
  if (!dlg.open) dlg.showModal();
  let days;
  try { days = (await api("/api/changes/days")).days; }
  catch (e) { box.textContent = "Could not load the activity log: " + e.message; return; }
  box.innerHTML = "";
  if (!days.length) { box.innerHTML = "<p class=\"muted\">Nothing yet - files show up here once a sync adds or changes them.</p>"; return; }
  for (const d of days) {
    const det = document.createElement("details");
    det.className = "day";
    det.innerHTML = "<summary><b></b><span class=\"chips\"></span></summary><div class=\"day-body muted small\">Loading…</div>";
    $("b", det).textContent = dayName(d.day);
    const chips = $(".chips", det);
    for (const k of KIND_ORDER) {
      if (!d.counts[k]) continue;
      const c = document.createElement("span");
      c.className = "chip " + KIND[k][1];
      c.textContent = `${num(d.counts[k])} ${KIND[k][0].toLowerCase()}` + (d.bytes[k] && k !== "failed" ? ` · ${bytes(d.bytes[k])}` : "");
      chips.append(c);
    }
    det.addEventListener("toggle", async () => {
      if (!det.open || det.dataset.loaded) return;
      det.dataset.loaded = "1";
      const bodyEl = $(".day-body", det);
      try {
        const r = await api(`/api/changes?day=${d.day}`);
        bodyEl.innerHTML = "";
        bodyEl.className = "day-body";
        for (const k of KIND_ORDER) {
          const list = r.files.filter((f) => f.kind === k).sort((x, y) => coll.compare(x.path, y.path));
          if (!list.length) continue;
          const h = document.createElement("h3");
          h.className = "kind-head " + KIND[k][1];
          h.textContent = `${KIND[k][0]} (${num(d.counts[k] || list.length)})`;
          const tbl = document.createElement("table");
          tbl.className = "files";
          tbl.innerHTML = `<thead><tr><th>File</th><th>Size</th><th>${k === "failed" || k === "deleted" ? "Why" : "Time"}</th></tr></thead><tbody></tbody>`;
          for (const f of list) tbl.tBodies[0].append(fileRow(f, f.note ? `${clock(f.t)} · ${f.note}` : clock(f.t)));
          bodyEl.append(h, tbl);
        }
        if (r.total > r.files.length) {
          const p = document.createElement("p");
          p.className = "muted small";
          p.textContent = `Showing the newest ${num(r.files.length)} of ${num(r.total)} changes this day.`;
          bodyEl.append(p);
        }
      } catch (e) { bodyEl.textContent = "Could not load this day: " + e.message; }
    });
    box.append(det);
  }
  box.querySelector("details").open = true;
}
$("#btn-changes").addEventListener("click", openChanges);

/* ---------------------------------------------------------------- Clean up */
const CLEAN_REASONS = {
  gone: ["No longer on the site", "khinsider doesn't list these any more (removed from their album, or the album is gone).", true],
  part: ["Unfinished downloads that aren't needed", "Partly downloaded files of something you no longer sync.", true],
  unticked: ["Not ticked any more", "Still on the site, but their album isn't ticked, or their format or the album images aren't synced. Leave these if you want to keep them.", false],
};
const CLEAN_SHOW = 500;               // rows drawn per group (the group's box still covers all)
let cleanFiles = [];
const cleanSel = new Set();
function renderLeftovers(l, busy) {
  const box = $("#leftover-box");
  if (l && l.files) {
    $("#leftover-text").textContent = `${nFiles(l.files)} (${bytes(l.bytes)}) in the download folder ${l.files === 1 ? "isn't" : "aren't"} part of the sync any more.`;
    box.classList.add("has");
  } else {
    $("#leftover-text").textContent = l ? "Nothing to clean up in the download folder." : "";
    box.classList.remove("has");
  }
  $("#btn-cleanup").textContent = l && l.files ? "Review" : "Clean up…";
}
function cleanCount() {
  let n = 0, b = 0;
  for (const f of cleanFiles) if (cleanSel.has(f.path)) { n++; b += f.size; }
  return [n, b];
}
function renderCleanFoot() {
  const [n, b] = cleanCount();
  $("#clean-sel").textContent = n ? `${num(n)} selected (${bytes(b)})` : "Nothing selected";
  $("#clean-delete").disabled = !n;
  for (const g of document.querySelectorAll("#clean-body .clean-group")) {
    const list = cleanFiles.filter((f) => f.reason === g.dataset.reason);
    const on = list.filter((f) => cleanSel.has(f.path)).length;
    const box = $("input.group", g);
    box.checked = on > 0 && on === list.length;
    box.indeterminate = on > 0 && on < list.length;
  }
}
function renderClean(d) {
  cleanFiles = d.files;
  cleanSel.clear();
  for (const f of cleanFiles) if (CLEAN_REASONS[f.reason][2]) cleanSel.add(f.path);
  const body = $("#clean-body");
  body.innerHTML = "";
  $("#clean-confirm").hidden = true;
  $("#clean-result").hidden = true;
  $("#clean-empty").hidden = !!cleanFiles.length;
  $("#clean-foot").hidden = !cleanFiles.length;
  for (const reason of Object.keys(CLEAN_REASONS)) {
    const list = cleanFiles.filter((f) => f.reason === reason);
    if (!list.length) continue;
    const [title, help] = CLEAN_REASONS[reason];
    const g = document.createElement("section");
    g.className = "clean-group";
    g.dataset.reason = reason;
    g.innerHTML = `<label class="check head"><input type="checkbox" class="group"><span><b></b> <span class="muted small"></span><br><small class="muted"></small></span></label><ul class="clean-list"></ul>`;
    $("b", g).textContent = title;
    $("span.muted", g).textContent = `${nFiles(list.length)}, ${bytes(list.reduce((a, f) => a + f.size, 0))}`;
    $("small", g).textContent = help;
    $("input.group", g).addEventListener("change", (e) => {
      for (const f of list) e.target.checked ? cleanSel.add(f.path) : cleanSel.delete(f.path);
      for (const c of g.querySelectorAll(".clean-list input")) c.checked = e.target.checked;
      renderCleanFoot();
    });
    const ul = $("ul", g);
    for (const f of list.slice(0, CLEAN_SHOW)) {
      const li = document.createElement("li");
      li.innerHTML = `<label class="check"><input type="checkbox"><span class="p"></span><span class="muted small"></span></label>`;
      const c = $("input", li);
      c.checked = cleanSel.has(f.path);
      c.addEventListener("change", () => { c.checked ? cleanSel.add(f.path) : cleanSel.delete(f.path); renderCleanFoot(); });
      $(".p", li).textContent = f.path;
      $(".p", li).title = f.path;
      $("span.muted", li).textContent = bytes(f.size);
      ul.append(li);
    }
    if (list.length > CLEAN_SHOW) {
      const li = document.createElement("li");
      li.className = "muted small";
      li.textContent = `… and ${num(list.length - CLEAN_SHOW)} more (included when the group is ticked)`;
      ul.append(li);
    }
    body.append(g);
  }
  renderCleanFoot();
}
async function openCleanup() {
  const dlg = $("#cleanup-dlg");
  if (!dlg.open) dlg.showModal();
  $("#clean-body").innerHTML = "";
  $("#clean-empty").hidden = true;
  $("#clean-foot").hidden = true;
  $("#clean-loading").hidden = false;
  try { renderClean(await api("/api/cleanup")); }
  catch (e) { $("#clean-body").textContent = "Could not check the download folder: " + e.message; }
  $("#clean-loading").hidden = true;
}
$("#btn-cleanup").addEventListener("click", openCleanup);
$("#clean-delete").addEventListener("click", () => {
  const [n, b] = cleanCount();
  $("#clean-confirm-text").textContent = `Delete ${nFiles(n)} (${bytes(b)}) from the download folder for good? This can't be undone.`;
  $("#clean-confirm-yes").textContent = `Delete ${nFiles(n)}`;
  $("#clean-confirm").hidden = false;
  $("#clean-foot").hidden = true;
  $("#clean-confirm-no").focus();
});
$("#clean-confirm-no").addEventListener("click", () => { $("#clean-confirm").hidden = true; $("#clean-foot").hidden = false; });
$("#clean-confirm-yes").addEventListener("click", async () => {
  const yes = $("#clean-confirm-yes");
  yes.disabled = true;
  const paths = cleanFiles.filter((f) => cleanSel.has(f.path)).map((f) => f.path);
  let msg, bad = false;
  try {
    const r = await api("/api/cleanup/delete", { method: "POST", body: { paths, confirm: true } });
    msg = `Deleted ${nFiles(r.deleted.length)} (${bytes(r.bytes)}).`;
    if (r.errors.length) { msg += ` ${r.errors.length} could not be deleted: ${r.errors.slice(0, 3).join("; ")}`; bad = true; }
    if (r.skipped) msg += ` ${num(r.skipped)} were skipped because they're part of the sync again or already gone.`;
    renderClean(await api("/api/cleanup"));
  } catch (e) { msg = "Nothing was deleted: " + e.message; bad = true; $("#clean-confirm").hidden = true; $("#clean-foot").hidden = false; }
  yes.disabled = false;
  const out = $("#clean-result");
  out.textContent = msg;
  out.className = "small " + (bad ? "error" : "ok");
  out.hidden = false;
  poll(true);
});

/* ---------------------------------------------------------------- theme */
function themeLabel() {
  $("#theme-label").textContent = document.documentElement.dataset.theme === "light" ? "Dark theme" : "Light theme";
}
$("#btn-theme").addEventListener("click", () => {
  const light = document.documentElement.dataset.theme !== "light";
  if (light) document.documentElement.dataset.theme = "light";
  else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("theme", light ? "light" : "dark"); } catch (e) {}
  themeLabel();
});
themeLabel();

/* ---------------------------------------------------------------- start */
$("#search").value = "";
$("#f-type").value = view.type;
if (view.show === "synced") view.show = "downloaded";          // the name before 1.0.2
$("#f-show").value = view.show;
loadPlatforms().then(() => reload()).catch(() => reload()).finally(() => poll());
