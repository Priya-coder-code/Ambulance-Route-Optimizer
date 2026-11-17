'use strict';
/* Ambulance Route Optimizer – front end.
   Two synchronised Leaflet maps (Dijkstra | A*) on OpenStreetMap tiles. */

const ALGOS = ['dijkstra', 'astar'];
const COLORS = { leg1: '#1f5fd6', leg2: '#c8102e', exp1: '#7fb2ff', exp2: '#ffb37a', jam: '#e8a800', blocked: '#111' };
const $ = (id) => document.getElementById(id);

const maps = {};
const layers = {};
let state = null;        // graph summary, hospitals, ambulances, traffic
let lastQuery = null;    // {type, ...} so traffic changes can re-route
let lastResult = null;
let routeStart = null;
let animToken = 0;
let roadsCache = null;
let syncing = false;

/* ---------- a fast canvas layer for thousands of "explored" dots ---------- */
const PointsLayer = L.Layer.extend({
  initialize(color) { this.color = color; this.pts = []; this.count = 0; },
  onAdd(map) {
    this._map = map;
    this._canvas = L.DomUtil.create('canvas', 'pts-layer');
    map.getPane('explored').appendChild(this._canvas);
    map.on('moveend zoomend resize viewreset', this._reset, this);
    map.on('zoomstart', this._hide, this);
    this._reset();
  },
  onRemove(map) {
    L.DomUtil.remove(this._canvas);
    map.off('moveend zoomend resize viewreset', this._reset, this);
    map.off('zoomstart', this._hide, this);
  },
  _hide() { this._canvas.style.display = 'none'; },
  setPoints(pts) { this.pts = pts || []; this.count = 0; this._reset(); },
  setCount(n) {
    n = Math.min(n, this.pts.length);
    if (n < this.count) { this.count = n; this._reset(); return; }
    const from = this.count; this.count = n; this._draw(from, n);
  },
  _reset() {
    const map = this._map; if (!map) return;
    const size = map.getSize(), c = this._canvas, dpr = window.devicePixelRatio || 1;
    c.width = size.x * dpr; c.height = size.y * dpr;
    c.style.width = size.x + 'px'; c.style.height = size.y + 'px'; c.style.display = '';
    L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
    this._ctx = c.getContext('2d');
    this._ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._draw(0, this.count);
  },
  _draw(a, b) {
    const ctx = this._ctx, map = this._map; if (!ctx) return;
    const r = map.getZoom() >= 16 ? 2.4 : map.getZoom() >= 14 ? 1.8 : 1.2;
    ctx.fillStyle = this.color;
    for (let i = a; i < b; i++) {
      const p = map.latLngToContainerPoint(this.pts[i]);
      ctx.fillRect(p.x - r, p.y - r, 2 * r, 2 * r);
    }
  },
});

/* ------------------------------------------------------------- maps */
function makeMap(algo) {
  const m = L.map('map-' + algo, { preferCanvas: true, zoomSnap: 0.5 });
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(m);
  m.createPane('explored').style.zIndex = 350;
  layers[algo] = {
    roads: L.layerGroup().addTo(m),
    exp1: new PointsLayer(COLORS.exp1).addTo(m),
    exp2: new PointsLayer(COLORS.exp2).addTo(m),
    static: L.layerGroup().addTo(m),
    run: L.layerGroup().addTo(m),
  };
  m.on('move', () => {
    if (syncing) return;
    syncing = true;
    for (const other of ALGOS) if (other !== algo) maps[other].setView(m.getCenter(), m.getZoom(), { animate: false });
    syncing = false;
  });
  m.on('click', (e) => onMapClick(e.latlng));
  maps[algo] = m;
}

const each = (fn) => ALGOS.forEach((a) => fn(a, maps[a], layers[a]));

function icon(cls, text, size = 22, anchor = null) {
  return L.divIcon({ className: '', html: `<div class="mk ${cls}">${text}</div>`, iconSize: [size, size],
                     iconAnchor: anchor || [size / 2, size / 2] });
}

/* ------------------------------------------------------------- utils */
async function api(path, body) {
  const opts = body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {};
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

let toastTimer = null;
function toast(msg, ms = 2600) {
  const t = $('toast'); t.textContent = msg; t.style.display = 'block';
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.style.display = 'none'), ms);
}

async function withBusy(text, fn) {
  $('busy-text').textContent = text; $('busy').style.display = 'grid';
  try { return await fn(); }
  catch (e) { toast('⚠ ' + e.message, 6000); throw e; }
  finally { $('busy').style.display = 'none'; }
}

const fmtTime = (s) => (s == null ? '–' : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
const fmtDist = (m) => (m == null ? '–' : m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`);
const fmtInt = (n) => n.toLocaleString('en-IN');
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const mode = () => document.querySelector('input[name=mode]:checked').value;

/* ------------------------------------------------- static map content */
const DETAIL_ZOOM = 14;   // below this, hospitals are small dots and ambulances are hidden

function renderStatic() {
  const detailed = maps.dijkstra.getZoom() >= DETAIL_ZOOM;
  renderStatic.detailed = detailed;
  each((a, m, L_) => {
    L_.static.clearLayers();
    if (state.jams.length) L.polyline(state.jams, { color: COLORS.jam, weight: 6, opacity: 0.8, interactive: false }).addTo(L_.static);
    if (state.blocked.length) L.polyline(state.blocked, { color: COLORS.blocked, weight: 6, dashArray: '2 7', interactive: false }).addTo(L_.static);
    for (const h of state.hospitals) {
      const mk = detailed
        ? L.marker([h.lat, h.lon], { icon: icon('hospital', 'H'), keyboard: false, bubblingMouseEvents: true })
        : L.circleMarker([h.lat, h.lon], { radius: 4, color: '#fff', weight: 1.5, fillColor: COLORS.leg2, fillOpacity: 1 });
      mk.bindTooltip(esc(h.name)).addTo(L_.static);
    }
    for (const am of state.ambulances) {
      if (!detailed && am.available) continue;
      L.marker([am.lat, am.lon], { icon: icon('ambulance' + (am.available ? '' : ' busy'), 'A', 18, [-3, -3]),
                                   keyboard: false, bubblingMouseEvents: true })
        .bindTooltip(esc(am.name) + (am.available ? '' : ' (busy)')).addTo(L_.static);
    }
  });
  const s = state.summary;
  $('area-name').textContent = s.name;
  $('graph-summary').innerHTML =
    `${s.size_km[0]} × ${s.size_km[1]} km · ${fmtInt(s.road_km)} km of road<br>` +
    `<b>${fmtInt(s.nodes)}</b> intersections (nodes) · <b>${fmtInt(s.edges)}</b> directed edges` +
    (s.raw_nodes ? ` <span title="OSM points before simplification">(${fmtInt(s.raw_nodes)} raw OSM points)</span>` : '') + '<br>' +
    `<b>${state.hospitals.length}</b> hospitals · <b>${state.ambulances.filter((x) => x.available).length}/${state.ambulances.length}</b> ambulances free` +
    (state.hospitals.length ? '' : '<br><span style="color:#c8102e">No hospitals found here – use “Add hospital”.</span>');
}

async function toggleRoads() {
  if (!$('show-roads').checked) { each((a, m, L_) => L_.roads.clearLayers()); return; }
  const m = maps.dijkstra;
  if (m.getZoom() < 13) {
    each((a, mp, L_) => L_.roads.clearLayers());
    toast('Zoom in further to see the road graph edges');
    return;
  }
  const b = m.getBounds();
  const key = [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((x) => x.toFixed(4)).join(',');
  if (roadsCache && roadsCache.key === key) return;
  const q = `s=${b.getSouth()}&w=${b.getWest()}&n=${b.getNorth()}&e=${b.getEast()}`;
  const res = await api('/api/roads?' + q);
  roadsCache = { key };
  each((a, mp, L_) => {
    L_.roads.clearLayers();
    L.polyline(res.roads, { color: '#3d4f7a', weight: 1.3, opacity: 0.6, interactive: false }).addTo(L_.roads);
  });
}

function clearRun() {
  animToken++;
  each((a, m, L_) => { L_.run.clearLayers(); L_.exp1.setPoints([]); L_.exp2.setPoints([]); });
  ALGOS.forEach((a) => ($('count-' + a).textContent = ''));
}

/* ------------------------------------------------------------ animation */
function playPhases(byAlgo) {
  const token = ++animToken;
  const frames = +$('speed').value;
  each((a, m, L_) => { L_.exp1.setPoints([]); L_.exp2.setPoints([]); for (const ph of byAlgo[a]) L_[ph.layer].setPoints(ph.pts); });
  const maxLen = Math.max(...ALGOS.map((a) => byAlgo[a].reduce((s, p) => s + p.pts.length, 0)), 1);
  const perFrame = frames === 0 ? Infinity : Math.max(1, Math.ceil(maxLen / frames));
  const st = {};
  ALGOS.forEach((a) => (st[a] = { phase: 0, count: 0, base: 0, done: byAlgo[a].length === 0 }));
  return new Promise((resolve) => {
    function step() {
      if (token !== animToken) return resolve(false);
      let allDone = true;
      for (const a of ALGOS) {
        const s = st[a], phases = byAlgo[a];
        let budget = perFrame;
        while (!s.done && budget > 0) {
          const ph = phases[s.phase];
          const take = Math.min(budget, ph.pts.length - s.count);
          s.count += take; budget -= take;
          layers[a][ph.layer].setCount(s.count);
          if (s.count >= ph.pts.length) {
            if (ph.onDone) ph.onDone();
            s.base += ph.real; s.phase++; s.count = 0;
            if (s.phase >= phases.length) s.done = true;
          }
        }
        const cur = s.done ? 0 : Math.round(s.count * phases[s.phase].real / Math.max(1, phases[s.phase].pts.length));
        $('count-' + a).textContent = `${fmtInt(s.base + cur)} nodes explored` + (s.done ? ' ✓' : '');
        if (!s.done) allDone = false;
      }
      if (allDone) resolve(true); else requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  });
}

function drawPath(a, coords, color) {
  if (!coords || coords.length < 2) return;
  L.polyline(coords, { color: '#fff', weight: 9, opacity: 0.9, interactive: false }).addTo(layers[a].run);
  L.polyline(coords, { color, weight: 5, opacity: 0.95, interactive: false }).addTo(layers[a].run);
}

function animateDispatch(res) {
  clearRun();
  const byAlgo = {};
  each((a, m, L_) => {
    const leg = res.legs[a];
    L.marker(res.incident, { icon: icon('incident', '!', 26), zIndexOffset: 1000, bubblingMouseEvents: true }).bindTooltip('Emergency').addTo(L_.run);
    byAlgo[a] = [
      { layer: 'exp1', pts: leg.to_patient.explored, real: leg.to_patient.nodes_explored, onDone: () => drawPath(a, leg.to_patient.path, COLORS.leg1) },
      { layer: 'exp2', pts: leg.to_hospital.explored, real: leg.to_hospital.nodes_explored, onDone: () => drawPath(a, leg.to_hospital.path, COLORS.leg2) },
    ];
  });
  return playPhases(byAlgo);
}

function animateRoute(res) {
  clearRun();
  const byAlgo = {};
  each((a, m, L_) => {
    const r = res.results[a];
    L.marker(res.from, { icon: icon('pin', 'A'), bubblingMouseEvents: true }).addTo(L_.run);
    L.marker(res.to, { icon: icon('pin', 'B'), bubblingMouseEvents: true }).addTo(L_.run);
    byAlgo[a] = [{ layer: 'exp2', pts: r.explored, real: r.nodes_explored, onDone: () => drawPath(a, r.path, COLORS.leg2) }];
  });
  return playPhases(byAlgo);
}

/* -------------------------------------------------------------- results */
function cmpTable(d, s) {
  const rows = [
    ['Nodes explored', 'nodes_explored'], ['Edges relaxed', 'edges_relaxed'],
    ['Heap pushes', 'heap_pushes'], ['Max frontier size', 'max_frontier'], ['Runtime (ms)', 'runtime_ms'],
  ];
  let html = '<table class="cmp"><thead><tr><th></th><th>Dijkstra</th><th>A*</th></tr></thead><tbody>';
  for (const [label, k] of rows) {
    const x = d[k], y = s[k];
    const f = k === 'runtime_ms' ? (v) => v.toFixed(2) : fmtInt;
    html += `<tr><td>${label}</td><td class="${x < y ? 'win' : ''}">${f(x)}</td><td class="${y < x ? 'win' : ''}">${f(y)}</td></tr>`;
  }
  html += '</tbody></table>';
  const pct = d.nodes_explored ? Math.round(100 * (1 - s.nodes_explored / d.nodes_explored)) : 0;
  const speed = s.runtime_ms > 0 ? d.runtime_ms / s.runtime_ms : 1;
  html += `<div class="saving">A* explored <b>${pct}% fewer nodes</b> and ran <b>${speed.toFixed(1)}× ${speed >= 1 ? 'faster' : 'slower'}</b>.</div>`;
  return html;
}

const sumStats = (a, b) => {
  const o = {};
  for (const k of ['nodes_explored', 'edges_relaxed', 'heap_pushes', 'runtime_ms']) o[k] = a[k] + b[k];
  o.max_frontier = Math.max(a.max_frontier, b.max_frontier);
  return o;
};

function samePill(ok) {
  return ok ? '<span class="pill ok">✓ Both algorithms found the same optimal route cost</span>'
            : '<span class="pill bad">Costs differ – check heuristic!</span>';
}

function renderDispatchResults(res) {
  const A = res.legs.astar, D = res.legs.dijkstra;
  const p = A.to_patient, h = A.to_hospital;
  if (!p.found || !h.found) {
    $('results').innerHTML = '<span class="pill bad">No route found – roads may be blocked.</span>';
    return;
  }
  const total = (p.time_s || 0) + (h.time_s || 0);
  let html = `<div class="trip">
    <div class="step"><span class="num" style="color:${COLORS.leg1}">1.</span><div><b>${esc(A.ambulance.name)}</b> → patient<br>
      <span class="muted">${fmtTime(p.time_s)} · ${fmtDist(p.distance_m)}</span></div></div>
    <div class="step"><span class="num" style="color:${COLORS.leg2}">2.</span><div>Patient → <b>${esc(A.hospital.name)}</b><br>
      <span class="muted">${fmtTime(h.time_s)} · ${fmtDist(h.distance_m)}</span></div></div>
    <div class="step" style="margin-top:8px"><span class="num">Total</span><div><b>${fmtTime(total)}</b> · ${fmtDist(p.distance_m + h.distance_m)}</div></div>
  </div>`;
  html += samePill(res.same_cost);
  html += '<h3 style="margin-top:14px">Search effort (both legs)</h3>';
  html += cmpTable(sumStats(D.to_patient, D.to_hospital), sumStats(p, h));
  html += `<details><summary>Per-leg breakdown</summary>
    <p><b>Leg 1 – nearest ambulance</b> (reverse-graph search from the patient)</p>${cmpTable(D.to_patient, p)}
    <p><b>Leg 2 – nearest hospital</b> (multi-target search)</p>${cmpTable(D.to_hospital, h)}
    <p class="muted">Roads: ${esc(h.roads.join(' → '))}</p></details>`;
  $('results').innerHTML = html;
}

function renderRouteResults(res) {
  const d = res.results.dijkstra, a = res.results.astar;
  if (!a.found) { $('results').innerHTML = '<span class="pill bad">No route between these points (roads blocked?)</span>'; return; }
  let html = `<div class="trip"><div class="step"><span class="num">A → B</span><div><b>${fmtTime(a.time_s)}</b> · ${fmtDist(a.distance_m)}<br>
    <span class="muted">${esc(a.roads.join(' → '))}</span></div></div></div>`;
  html += samePill(res.same_cost);
  html += '<h3 style="margin-top:14px">Search effort</h3>' + cmpTable(d, a);
  $('results').innerHTML = html;
}

/* ----------------------------------------------------------- actions */
const metric = () => $('metric').value;

async function runDispatch(latlng, markBusy) {
  const res = await withBusy('Dispatching: nearest ambulance & hospital…', () =>
    api('/api/dispatch', { incident: [latlng.lat, latlng.lng], metric: metric(), mark_busy: markBusy }));
  lastQuery = { type: 'dispatch', latlng }; lastResult = res;
  state.ambulances = res.ambulances; renderStatic();
  renderDispatchResults(res);
  await animateDispatch(res);
}

async function runRoute(from, to) {
  const res = await withBusy('Finding route…', () =>
    api('/api/route', { from: [from.lat, from.lng], to: [to.lat, to.lng], metric: metric() }));
  lastQuery = { type: 'route', from, to }; lastResult = res;
  renderRouteResults(res);
  await animateRoute(res);
}

async function rerunLast() {
  if (!lastQuery) return;
  if (lastQuery.type === 'dispatch') await runDispatch(lastQuery.latlng, false);
  else await runRoute(lastQuery.from, lastQuery.to);
}

async function onMapClick(latlng) {
  if (!state) return;
  const m = mode();
  try {
    if (m === 'dispatch') {
      await runDispatch(latlng, $('mark-busy').checked);
    } else if (m === 'route') {
      if (!routeStart) {
        clearRun(); routeStart = latlng;
        each((a, mp, L_) => L.marker(latlng, { icon: icon('pin', 'A'), bubblingMouseEvents: true }).addTo(L_.run));
        toast('Start set – now click the destination');
      } else {
        const from = routeStart; routeStart = null;
        await runRoute(from, latlng);
      }
    } else if (m === 'block' || m === 'jam') {
      state = await api('/api/traffic', { action: m, lat: latlng.lat, lon: latlng.lng });
      renderStatic();
      toast(m === 'block' ? 'Road blocked – re-routing…' : 'Traffic jam added – re-routing…');
      await rerunLast();
    } else if (m === 'hospital' || m === 'ambulance') {
      state = await api('/api/place', { kind: m, lat: latlng.lat, lon: latlng.lng });
      renderStatic();
      toast(m === 'hospital' ? 'Hospital added' : 'Ambulance base added');
    }
  } catch (e) { console.error(e); }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function loadArea() {
  const place = $('place').value.trim();
  if (!place) { toast('Type a place name first'); return; }
  try { await api('/api/load', { place, size_km: +$('size').value }); }
  catch (e) { toast('⚠ ' + e.message, 6000); return; }
  $('busy').style.display = 'grid';
  $('busy-text').textContent = 'Downloading roads & hospitals from OpenStreetMap…';
  try {
    for (;;) {
      await sleep(1000);
      const st = await api('/api/load_status');
      if (st.log.length) $('busy-text').innerHTML = st.log.map(esc).join('<br>');
      if (st.loading) continue;
      if (st.error) throw new Error(st.error);
      state = st.state; roadsCache = null; lastQuery = null; lastResult = null;
      clearRun(); fit(); renderStatic(); toggleRoads();
      $('results').innerHTML = '<p class="muted">Click on the map to report an emergency.</p>';
      toast(`Loaded ${fmtInt(state.summary.nodes)} intersections, ${state.hospitals.length} hospitals`);
      return;
    }
  } catch (e) { toast('⚠ ' + e.message, 8000); }
  finally { $('busy').style.display = 'none'; }
}

function fit() { maps.dijkstra.fitBounds(state.bounds, { padding: [10, 10] }); }

/* --------------------------------------------------------------- boot */
async function init() {
  ALGOS.forEach(makeMap);
  $('load-btn').onclick = loadArea;
  $('place').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadArea(); });
  $('show-roads').onchange = () => { roadsCache = null; toggleRoads(); };
  maps.dijkstra.on('zoomend', () => {
    if (state && (maps.dijkstra.getZoom() >= DETAIL_ZOOM) !== renderStatic.detailed) renderStatic();
  });
  maps.dijkstra.on('moveend', () => { if ($('show-roads').checked) toggleRoads().catch(() => {}); });
  $('clear-traffic').onclick = async () => { state = await api('/api/traffic', { action: 'clear' }); renderStatic(); await rerunLast(); };
  $('free-amb').onclick = async () => { state = await api('/api/place', { kind: 'reset_ambulances' }); renderStatic(); toast('All ambulances available'); };
  $('replay').onclick = () => {
    if (!lastResult) return;
    if (lastQuery.type === 'dispatch') animateDispatch(lastResult); else animateRoute(lastResult);
  };
  document.querySelectorAll('input[name=mode]').forEach((r) => (r.onchange = () => { routeStart = null; }));
  state = await api('/api/state');
  fit(); renderStatic();
}

init();
