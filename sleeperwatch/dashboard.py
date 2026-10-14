"""Build a self-contained, interactive HTML dashboard from the fares database.

Everything (data, styles, script) is inlined into one file, so it opens
offline in any browser and can be regenerated after every `fetch`.

Main question it answers: which travel date (and train) is cheapest for a
route? Secondary: how did one departure's price move as it approached?
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def collect(db: sqlite3.Connection) -> dict:
    """Group observations into departures (route x party size x room x train x
    travel date), each with its price history. Where several fare families
    or overlapping watches produced rows for the same check, the cheapest
    price at that check is kept."""
    rows = db.execute(
        """SELECT origin, destination, passengers,
                  COALESCE(NULLIF(accommodation, ''), travel_class) AS room,
                  train_number, MAX(train_name) AS train_name, travel_date,
                  MAX(depart) AS depart, observed_at, MIN(price) AS price,
                  MIN(days_out) AS days_out, MIN(seats_left) AS seats_left
           FROM observations
           GROUP BY origin, destination, passengers, room, train_number, travel_date, observed_at
           ORDER BY observed_at""").fetchall()
    deps: dict[tuple, dict] = {}
    for r in rows:
        key = (r["origin"], r["destination"], r["passengers"], r["room"], r["train_number"], r["travel_date"])
        d = deps.setdefault(key, {
            "route": f"{r['origin']}→{r['destination']}", "pax": r["passengers"], "room": r["room"],
            "train": r["train_number"], "name": r["train_name"] or "", "date": r["travel_date"],
            "depart": (r["depart"] or "")[11:16], "obs": [],
        })
        d["obs"].append([r["observed_at"], r["days_out"], r["price"], r["seats_left"]])
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "departures": list(deps.values()),
    }


def render(data: dict) -> str:
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return _TEMPLATE.replace("__DATA__", payload)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sleeperwatch Fares</title>
<style>
:root {
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10); --wash: rgba(11,11,11,0.04);
  --select: rgba(42,120,214,0.10);
  --s1: #2a78d6; --s2: #eb6834; --s3: #1baf7a; --s4: #eda100; --s5: #e87ba4; --s6: #008300; --s7: #6250d6; --s8: #e34948;
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10); --wash: rgba(255,255,255,0.05);
    --select: rgba(57,135,229,0.18);
    --s1: #3987e5; --s2: #d95926; --s3: #199e70; --s4: #c98500; --s5: #d55181; --s6: #008300; --s7: #9085e9; --s8: #e66767;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1120px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 20px; }
.filters { display: flex; flex-wrap: wrap; gap: 12px 20px; align-items: end; margin-bottom: 16px; }
.filters label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--ink-2); }
.filters label.check { flex-direction: row; align-items: center; gap: 6px; font-size: 13px; color: var(--ink); padding-bottom: 6px; }
select { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--ring);
         border-radius: 8px; padding: 6px 28px 6px 10px; min-width: 120px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
.card h2 { font-size: 14px; font-weight: 600; margin: 0 0 2px; }
.card .note { color: var(--ink-2); font-size: 12px; margin: 0 0 12px; }
.hero { display: flex; flex-wrap: wrap; gap: 8px 32px; align-items: baseline; }
.hero .label { color: var(--ink-2); font-size: 13px; width: 100%; }
.hero .figure { font-size: 48px; font-weight: 600; line-height: 1.05; }
.hero .what { font-size: 15px; }
.hero .what span { color: var(--ink-2); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 8px; font-size: 13px; color: var(--ink-2); }
.legend .key { display: inline-block; width: 16px; height: 2px; border-radius: 1px; vertical-align: middle; margin-right: 6px; }
.chart { position: relative; }
.chart svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart text { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.tip { position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--ring);
       border-radius: 8px; padding: 8px 10px; font-size: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.12);
       min-width: 170px; display: none; z-index: 2; }
.tip .head { color: var(--ink-2); margin-bottom: 4px; }
.tip .row { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.tip .row b { font-variant-numeric: tabular-nums; min-width: 52px; }
.tip .row .key { width: 12px; height: 2px; border-radius: 1px; flex: none; }
.tip .row span { color: var(--ink-2); }
.table-wrap { overflow: auto; max-height: 460px; border-bottom: 1px solid var(--grid); }
thead th { position: sticky; top: 0; background: var(--surface); z-index: 1; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; font-weight: 500; color: var(--ink-2); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--axis); white-space: nowrap; }
td { padding: 7px 8px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
th.num, td.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr { cursor: pointer; }
tbody tr:hover { background: var(--wash); }
tbody tr[aria-selected="true"] { background: var(--select); }
tbody tr:focus-visible { outline: 2px solid var(--s1); outline-offset: -2px; }
td .key { display: inline-block; width: 12px; height: 2px; border-radius: 1px; vertical-align: middle; margin-right: 6px; }
.rank { color: var(--muted); }
.empty { color: var(--ink-2); padding: 24px 0; text-align: center; }
.hidden { display: none; }
@media (max-width: 640px) { .hero .figure { font-size: 40px; } }
</style>
</head>
<body>
<main>
  <h1>Sleeperwatch fares</h1>
  <p class="sub" id="generated"></p>

  <div class="filters" role="group" aria-label="Filters">
    <label>Route <select id="f-route"></select></label>
    <label>Room <select id="f-room"></select></label>
    <label>Travelers <select id="f-pax"></select></label>
    <label>Price shown
      <select id="f-metric">
        <option value="avg">Average of checks</option>
        <option value="latest">Latest check</option>
        <option value="low">Lowest seen</option>
      </select>
    </label>
    <label class="check"><input type="checkbox" id="f-past"> Include past dates</label>
  </div>

  <div id="content">
    <section class="card" aria-live="polite">
      <div class="hero">
        <div class="label" id="hero-label">Cheapest departure</div>
        <div class="figure" id="hero-figure"></div>
        <div class="what" id="hero-what"></div>
      </div>
    </section>

    <section class="card">
      <h2>Price by travel date</h2>
      <p class="note" id="chart-note"></p>
      <div class="legend" id="legend"></div>
      <div class="chart" id="chart"><div class="tip" id="tip"></div></div>
    </section>

    <section class="card">
      <h2>Departures, cheapest first</h2>
      <p class="note" id="table-note">Ranked by average price across all checks. Select a departure to see how its price moved.</p>
      <div class="table-wrap"><table>
        <thead><tr>
          <th class="num">#</th><th>Travel date</th><th>Train</th><th>Departs</th>
          <th class="num">Average</th><th class="num">Latest</th><th class="num">Low</th><th class="num">High</th>
          <th class="num">Checks</th><th class="num">Last checked</th>
        </tr></thead>
        <tbody id="rows"></tbody>
      </table></div>
    </section>

    <section class="card hidden" id="detail">
      <h2 id="detail-title"></h2>
      <p class="note">Price at each check, by days before departure. Left is further out; right is closer to the trip.</p>
      <div class="chart" id="detail-chart"><div class="tip" id="detail-tip"></div></div>
    </section>
  </div>
  <p class="empty hidden" id="empty">No fares recorded yet. Run <code>sleeperwatch fetch</code>, then rebuild this page with <code>sleeperwatch dashboard</code>.</p>
</main>

<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const $ = id => document.getElementById(id);
const money = v => "$" + Math.round(v).toLocaleString("en-US");
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const parseDay = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const fmtDay = s => { const d = parseDay(s); return `${DOW[d.getDay()]} ${MON[d.getMonth()]} ${d.getDate()}`; };
const todayStr = (() => { const t = new Date(); return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`; })();
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// Summaries per departure.
for (const d of DATA.departures) {
  const p = d.obs.map(o => o[2]);
  d.avg = p.reduce((a, b) => a + b, 0) / p.length;
  d.low = Math.min(...p); d.high = Math.max(...p);
  d.latest = p[p.length - 1];
  d.lastAt = d.obs[d.obs.length - 1][0];
  d.n = p.length;
  d.label = `#${d.train}` + (d.name ? ` ${d.name}` : "");
}

// Colour follows the train, never its rank or the current filter.
const trains = [...new Set(DATA.departures.map(d => d.train))]
  .sort((a, b) => (parseInt(a) - parseInt(b)) || a.localeCompare(b));
// Resolved to a hex value: SVG presentation attributes don't resolve var().
const colorOf = t => cssVar(`--s${(trains.indexOf(t) % 8) + 1}`);

function fillSelect(sel, values, fmt = v => v) {
  const prev = sel.value;
  sel.replaceChildren(...values.map(v => { const o = document.createElement("option"); o.value = v; o.textContent = fmt(v); return o; }));
  if (values.map(String).includes(prev)) sel.value = prev;
}

const F = { route: $("f-route"), room: $("f-room"), pax: $("f-pax"), metric: $("f-metric"), past: $("f-past") };
let selectedKey = null;

function refreshFilterOptions() {
  const all = DATA.departures;
  fillSelect(F.route, [...new Set(all.map(d => d.route))].sort());
  const r = all.filter(d => d.route === F.route.value);
  const rooms = [...new Set(r.map(d => d.room))].sort((a, b) => a === "Roomette" ? -1 : b === "Roomette" ? 1 : a.localeCompare(b));
  fillSelect(F.room, rooms);
  const rr = r.filter(d => d.room === F.room.value);
  fillSelect(F.pax, [...new Set(rr.map(d => d.pax))].sort((a, b) => a - b), v => v == 1 ? "1 traveler" : `${v} travelers`);
}

function current() {
  return DATA.departures.filter(d =>
    d.route === F.route.value && d.room === F.room.value && String(d.pax) === F.pax.value &&
    (F.past.checked || d.date >= todayStr));
}

function render() {
  const deps = current();
  const m = F.metric.value;
  const metricName = { avg: "average of checks", latest: "latest check", low: "lowest seen" }[m];

  // Hero: cheapest departure by the chosen metric.
  const best = [...deps].sort((a, b) => a[m] - b[m])[0];
  $("hero-label").textContent = `Cheapest departure (${metricName})`;
  $("hero-figure").textContent = best ? money(best[m]) : "–";
  const what = $("hero-what"); what.replaceChildren();
  if (best) {
    what.append(fmtDay(best.date) + " · " + best.label);
    const s = document.createElement("span");
    s.textContent = ` · ${best.room}, ${best.pax == 1 ? "1 traveler" : best.pax + " travelers"}`;
    what.append(s);
  }

  drawDateChart(deps, m, metricName);
  drawTable(deps);
  const sel = deps.find(d => keyOf(d) === selectedKey);
  if (sel) drawDetail(sel); else { $("detail").classList.add("hidden"); selectedKey = null; }
}

const keyOf = d => [d.route, d.pax, d.room, d.train, d.date].join("|");

// ---- shared SVG helpers -------------------------------------------------
function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
function niceTicks(lo, hi, count = 5) {
  if (lo === hi) { lo -= 50; hi += 50; }
  const step0 = (hi - lo) / count, mag = Math.pow(10, Math.floor(Math.log10(step0)));
  const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => s >= step0);
  const start = Math.floor(lo / step) * step, end = Math.ceil(hi / step) * step;
  const ticks = []; for (let v = start; v <= end + 1e-9; v += step) ticks.push(v);
  return ticks;
}
function frame(container, W, H, M) {
  container.querySelectorAll("svg").forEach(s => s.remove());
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" });
  container.insertBefore(svg, container.firstChild);
  return svg;
}
function yAxis(svg, ticks, y, M, W) {
  for (const t of ticks) {
    el("line", { x1: M.l, x2: W - M.r, y1: y(t), y2: y(t), stroke: cssVar("--grid"), "stroke-width": 1 }, svg);
    const tx = el("text", { x: M.l - 8, y: y(t) + 4, "text-anchor": "end" }, svg);
    tx.textContent = t >= 1000 ? "$" + (t / 1000).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "K" : "$" + t;
  }
}
function showTip(tip, container, px, py, build) {
  tip.replaceChildren(); build(tip); tip.style.display = "block";
  const cw = container.clientWidth, tw = tip.offsetWidth;
  let left = px + 14; if (left + tw > cw) left = px - tw - 14;
  tip.style.left = Math.max(0, left) + "px"; tip.style.top = Math.max(0, py - 10) + "px";
}
function tipRow(tip, color, value, label) {
  const r = document.createElement("div"); r.className = "row";
  const k = document.createElement("i"); k.className = "key"; k.style.background = color;
  const b = document.createElement("b"); b.textContent = value;
  const s = document.createElement("span"); s.textContent = label;
  r.append(k, b, s); tip.appendChild(r);
}

// ---- price by travel date --------------------------------------------------
function drawDateChart(deps, m, metricName) {
  const box = $("chart"), tip = $("tip");
  tip.style.display = "none";
  const series = {};
  for (const d of deps) (series[d.train] ??= { train: d.train, label: d.label, pts: [] }).pts.push(d);
  const list = Object.values(series).sort((a, b) => trains.indexOf(a.train) - trains.indexOf(b.train));
  list.forEach(s => s.pts.sort((a, b) => a.date.localeCompare(b.date)));

  $("chart-note").textContent = deps.length
    ? `Each point is one departure; price is the ${metricName}. Hover for details.`
    : "No departures match these filters.";
  const legend = $("legend"); legend.replaceChildren();
  if (list.length > 1) for (const s of list) {
    const item = document.createElement("span");
    const k = document.createElement("i"); k.className = "key"; k.style.background = colorOf(s.train);
    item.append(k, s.label); legend.appendChild(item);
  }
  const W = Math.max(box.clientWidth, 320), H = 300, M = { l: 56, r: 16, t: 12, b: 32 };
  const svg = frame(box, W, H, M);
  if (!deps.length) return;

  const dates = [...new Set(deps.map(d => d.date))].sort();
  const t0 = parseDay(dates[0]).getTime(), t1 = parseDay(dates[dates.length - 1]).getTime();
  const span = Math.max(t1 - t0, 864e5);
  const pad = dates.length === 1 ? 864e5 * 3 : span * 0.03;
  const x = s => M.l + ((parseDay(s).getTime() - (t0 - pad)) / (span + 2 * pad)) * (W - M.l - M.r);
  const vals = deps.map(d => d[m]);
  const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
  const y = v => M.t + (1 - (v - ticks[0]) / (ticks[ticks.length - 1] - ticks[0])) * (H - M.t - M.b);
  yAxis(svg, ticks, y, M, W);
  el("line", { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: cssVar("--axis"), "stroke-width": 1 }, svg);

  // X ticks: thin out to fit.
  const maxTicks = Math.max(2, Math.floor((W - M.l - M.r) / 80));
  const every = Math.ceil(dates.length / maxTicks);
  dates.forEach((d, i) => {
    if (i % every) return;
    const tx = el("text", { x: x(d), y: H - M.b + 18, "text-anchor": "middle" }, svg);
    const dd = parseDay(d); tx.textContent = `${MON[dd.getMonth()]} ${dd.getDate()}`;
  });

  const cross = el("line", { y1: M.t, y2: H - M.b, stroke: cssVar("--axis"), "stroke-width": 1, visibility: "hidden" }, svg);
  for (const s of list) {
    const c = colorOf(s.train);
    if (s.pts.length > 1)
      el("path", { d: s.pts.map((p, i) => `${i ? "L" : "M"}${x(p.date)},${y(p[m])}`).join(""), fill: "none",
                   stroke: c, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    for (const p of s.pts)
      el("circle", { cx: x(p.date), cy: y(p[m]), r: 4, fill: c, stroke: cssVar("--surface"), "stroke-width": 2 }, svg);
  }

  // Crosshair snaps to the nearest travel date; tooltip lists every train that day.
  const hit = el("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" }, svg);
  const move = ev => {
    const r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * (W / r.width);
    const date = dates.reduce((a, b) => Math.abs(x(b) - sx) < Math.abs(x(a) - sx) ? b : a);
    cross.setAttribute("x1", x(date)); cross.setAttribute("x2", x(date)); cross.setAttribute("visibility", "visible");
    const here = deps.filter(d => d.date === date).sort((a, b) => a[m] - b[m]);
    showTip(tip, box, x(date) * (r.width / W), ev.clientY - box.getBoundingClientRect().top, t => {
      const h = document.createElement("div"); h.className = "head"; h.textContent = fmtDay(date); t.appendChild(h);
      for (const d of here) tipRow(t, colorOf(d.train), money(d[m]), `${d.label} · ${d.n} check${d.n > 1 ? "s" : ""}`);
    });
  };
  hit.addEventListener("pointermove", move);
  hit.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
  hit.addEventListener("click", ev => {
    const r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * (W / r.width);
    const date = dates.reduce((a, b) => Math.abs(x(b) - sx) < Math.abs(x(a) - sx) ? b : a);
    const best = deps.filter(d => d.date === date).sort((a, b) => a[m] - b[m])[0];
    if (best) select(best);
  });
}

// ---- ranked table ------------------------------------------------------------
function drawTable(deps) {
  const tb = $("rows"); tb.replaceChildren();
  $("table-note").textContent = `${deps.length} departure${deps.length === 1 ? "" : "s"}, ranked by average price across all checks. Select one to see how its price moved.`;
  const sorted = [...deps].sort((a, b) => a.avg - b.avg || a.date.localeCompare(b.date));
  sorted.forEach((d, i) => {
    const tr = document.createElement("tr");
    tr.tabIndex = 0; tr.setAttribute("aria-selected", keyOf(d) === selectedKey ? "true" : "false");
    const cells = [
      [String(i + 1), "num rank"], [fmtDay(d.date), ""], [null, ""], [d.depart, ""],
      [money(d.avg), "num"], [money(d.latest), "num"], [money(d.low), "num"], [money(d.high), "num"],
      [String(d.n), "num"], [d.obs[d.obs.length - 1][1] != null ? `${Math.round(d.obs[d.obs.length - 1][1])} days out` : "", "num"],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td"); if (cls) td.className = cls;
      if (text === null) {
        const k = document.createElement("i"); k.className = "key"; k.style.background = colorOf(d.train);
        td.append(k, d.label);
      } else td.textContent = text;
      tr.appendChild(td);
    }
    tr.addEventListener("click", () => select(d));
    tr.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(d); } });
    tb.appendChild(tr);
  });
}

function select(d) {
  selectedKey = keyOf(d);
  for (const tr of $("rows").children) tr.setAttribute("aria-selected", "false");
  drawTable(current());
  drawDetail(d);
  $("detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---- one departure's history -----------------------------------------------
function drawDetail(d) {
  $("detail").classList.remove("hidden");
  $("detail-title").textContent = `${fmtDay(d.date)} · ${d.label} · ${d.room}`;
  const box = $("detail-chart"), tip = $("detail-tip"); tip.style.display = "none";
  const pts = d.obs.filter(o => o[1] != null).map(o => ({ at: o[0], x: o[1], p: o[2], left: o[3] }));
  const W = Math.max(box.clientWidth, 320), H = 220, M = { l: 56, r: 16, t: 12, b: 32 };
  const svg = frame(box, W, H, M);
  if (!pts.length) return;
  const xs = pts.map(p => p.x);
  let hiX = Math.max(...xs), loX = Math.min(0, ...xs);
  if (hiX - loX < 1) hiX = loX + 1;
  const x = v => M.l + (1 - (v - loX) / (hiX - loX)) * (W - M.l - M.r);   // further out on the left
  const ticks = niceTicks(Math.min(...pts.map(p => p.p)), Math.max(...pts.map(p => p.p)), 4);
  const y = v => M.t + (1 - (v - ticks[0]) / (ticks[ticks.length - 1] - ticks[0])) * (H - M.t - M.b);
  yAxis(svg, ticks, y, M, W);
  el("line", { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: cssVar("--axis"), "stroke-width": 1 }, svg);
  for (const v of niceTicks(loX, hiX, 5).filter(v => v >= loX && v <= hiX)) {
    const tx = el("text", { x: x(v), y: H - M.b + 18, "text-anchor": "middle" }, svg);
    tx.textContent = v === 0 ? "departure" : `${v}d`;
  }
  const c = colorOf(d.train);
  const path = pts.map((p, i) => i ? `H${x(p.x)}V${y(p.p)}` : `M${x(p.x)},${y(p.p)}`).join("");
  if (pts.length > 1) el("path", { d: path, fill: "none", stroke: c, "stroke-width": 2, "stroke-linejoin": "round" }, svg);
  for (const p of pts) el("circle", { cx: x(p.x), cy: y(p.p), r: 4, fill: c, stroke: cssVar("--surface"), "stroke-width": 2 }, svg);
  const last = pts[pts.length - 1];
  const lab = el("text", { x: x(last.x) + 8, y: y(last.p) - 8, "text-anchor": x(last.x) > W - 80 ? "end" : "start" }, svg);
  lab.textContent = money(last.p); lab.style.fill = cssVar("--ink-2");

  const hit = el("rect", { x: M.l, y: M.t, width: W - M.l - M.r, height: H - M.t - M.b, fill: "transparent" }, svg);
  const cross = el("line", { y1: M.t, y2: H - M.b, stroke: cssVar("--axis"), "stroke-width": 1, visibility: "hidden" }, svg);
  svg.insertBefore(cross, hit);
  hit.addEventListener("pointermove", ev => {
    const r = svg.getBoundingClientRect(), sx = (ev.clientX - r.left) * (W / r.width);
    const p = pts.reduce((a, b) => Math.abs(x(b.x) - sx) < Math.abs(x(a.x) - sx) ? b : a);
    cross.setAttribute("x1", x(p.x)); cross.setAttribute("x2", x(p.x)); cross.setAttribute("visibility", "visible");
    showTip(tip, box, x(p.x) * (r.width / W), ev.clientY - box.getBoundingClientRect().top, t => {
      const h = document.createElement("div"); h.className = "head";
      h.textContent = `${p.x.toFixed(1)} days out · checked ${p.at.slice(0, 16).replace("T", " ")} UTC`; t.appendChild(h);
      tipRow(t, c, money(p.p), p.left != null ? `${p.left} left` : "");
    });
  });
  hit.addEventListener("pointerleave", () => { tip.style.display = "none"; cross.setAttribute("visibility", "hidden"); });
}

// ---- wiring ------------------------------------------------------------------
$("generated").textContent = `Built ${DATA.generated.slice(0, 16).replace("T", " ")} UTC from ${DATA.departures.length} tracked departure${DATA.departures.length === 1 ? "" : "s"}. Re-run sleeperwatch dashboard after fetching to refresh.`;
if (!DATA.departures.length) {
  $("content").classList.add("hidden"); $("empty").classList.remove("hidden");
  document.querySelector(".filters").classList.add("hidden");
} else {
  // Default: show upcoming departures; if there are none, include past ones.
  F.past.checked = !DATA.departures.some(d => d.date >= todayStr);
  refreshFilterOptions();
  F.route.addEventListener("change", () => { refreshFilterOptions(); render(); });
  F.room.addEventListener("change", () => { refreshFilterOptions(); render(); });
  for (const f of [F.pax, F.metric, F.past]) f.addEventListener("change", render);
  render();
  let raf; window.addEventListener("resize", () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(render); });
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
}
</script>
</body>
</html>
"""
