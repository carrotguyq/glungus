#!/usr/bin/env python3
"""
EarthMC fall finder: towns and nations about to change hands or fall.

Rules this models (EarthMC, Nostra):
  * A mayor who hasn't logged in for 42 days loses the town at the next new day
    (13:00 GMT+3 = 10:00 UTC).
  * The town goes to the active councillor who has been in the town longest.
    Inactive councillors are skipped. With no active councillor it goes to the
    most active resident (approximated here by most recent login).
  * With nobody active left, the town falls.
  * A nation is inactive when every one of its towns is going to fall; it falls
    when the last town does.
Premium mayors keep their town forever, but the API doesn't show Premium, so a
mayor who is already past the deadline and still mayor is skipped as probably Premium.

Usage:
    pip install requests
    python earthmc_fall_finder.py                       # next 24h to the terminal
    python earthmc_fall_finder.py --html falling.html   # web page (covers next 14 days)
    python earthmc_fall_finder.py --html falling.html --html-hours 168
    python earthmc_fall_finder.py --webhook https://discord.com/api/webhooks/...
    python earthmc_fall_finder.py --loop 60             # re-check every 60 min
"""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.earthmc.net/v4"
INACTIVE_DAYS = 42
NEWDAY_HOUR = 10        # UTC hour of the daily new day (13:00 GMT+3)
BATCH = 100             # lower this if the API rejects big batches
DELAY = 0.4             # ~150 req/min, under the 180/min limit

session = requests.Session()
session.headers["User-Agent"] = "earthmc-fall-finder/1.2"


def call(method, path, **kw):
    for attempt in range(6):
        r = session.request(method, API + path, timeout=30, **kw)
        if r.status_code == 429:  # rate limited, back off
            time.sleep(5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Rate limited too long, try again later")


def post_batches(path, queries, template):
    out = []
    for i in range(0, len(queries), BATCH):
        out += call("POST", path, json={"query": queries[i:i + BATCH], "template": template})
        time.sleep(DELAY)
    return out


def next_newday(ts_ms):
    """First new day (10:00 UTC by default) at or after the given time."""
    dt = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
    day = dt.replace(hour=NEWDAY_HOUR, minute=0, second=0, microsecond=0)
    if day < dt:
        day += timedelta(days=1)
    return day.timestamp() * 1000


def find_falling(hours):
    """Return (towns, nations) with something happening within `hours` hours."""
    now = time.time() * 1000
    window = hours * 3600 * 1000
    inactive = INACTIVE_DAYS * 86400 * 1000

    print("Fetching towns...")
    index = call("GET", "/towns")
    all_towns = post_batches(
        "/towns",
        [t["uuid"] for t in index],
        {"name": True, "uuid": True, "mayor": True, "nation": True, "residents": True,
         "ranks": True, "status": True, "stats": True},
    )
    ruined = {t["uuid"] for t in all_towns if (t.get("status") or {}).get("isRuined")}
    towns = [t for t in all_towns if t["uuid"] not in ruined]

    uuids = sorted({r["uuid"] for t in towns for r in t.get("residents", [])})
    print(f"Fetching {len(uuids)} residents (~{len(uuids) // BATCH + 1} requests)...")
    players = post_batches("/players", uuids, {"uuid": True, "timestamps": True, "status": True})

    last_seen, joined = {}, {}
    for p in players:
        if (p.get("status") or {}).get("isNPC"):
            continue
        ts = (p.get("timestamps") or {}).get("lastOnline")
        if (p.get("status") or {}).get("isOnline"):
            ts = now
        if ts:
            last_seen[p["uuid"]] = ts
        j = (p.get("timestamps") or {}).get("joinedTownAt")
        if j:
            joined[p["uuid"]] = j

    town_fall = {}   # town uuid -> when it falls (ms), or None if it won't / unknown
    results, skipped = [], 0
    for t in towns:
        res = t.get("residents", [])
        mayor_id = (t.get("mayor") or {}).get("uuid")
        known = {r["uuid"]: last_seen[r["uuid"]] for r in res if r["uuid"] in last_seen}
        # Opted-out players are hidden by the API, so we can't be sure
        if not res or len(known) < len(res) or mayor_id not in known:
            town_fall[t["uuid"]] = None
            skipped += 1
            continue

        mayor_at = next_newday(known[mayor_id] + inactive)   # mayor loses the town
        if mayor_at <= now:   # past the deadline but still mayor: probably Premium
            town_fall[t["uuid"]] = None
            continue
        town_fall[t["uuid"]] = next_newday(max(known.values()) + inactive)
        if mayor_at > now + window:
            continue

        names = {r["uuid"]: r["name"] for r in res}
        by_name = {r["name"]: r["uuid"] for r in res}
        councillors = set()
        for e in (t.get("ranks") or {}).get("Councillor", []):
            # v4 may list councillors as objects ({name, uuid}) or plain names/uuids
            if isinstance(e, dict):
                u = e.get("uuid") or by_name.get(e.get("name"))
            else:
                u = by_name.get(e) or e
            if u in names:
                councillors.add(u)
        # who is still active when the mayor's time runs out
        active = [u for u, ts in known.items() if u != mayor_id and ts + inactive > mayor_at]
        active_c = [u for u in active if u in councillors]
        if active_c:
            succ = min(active_c, key=lambda u: joined.get(u, float("inf")))
            kind, outcome = "transfer", f"To {names[succ]} (councillor)"
        elif active:
            succ = max(active, key=lambda u: known[u])
            kind, outcome = "transfer", f"To {names[succ]} (most active)"
        else:
            succ = None
            kind, outcome = "falls", "Falls"

        players_info = sorted(
            ({"name": names[u],
              "role": "mayor" if u == mayor_id else "councillor" if u in councillors else "",
              "last": ts,
              "lapse": next_newday(ts + inactive),
              "next": u == succ}
             for u, ts in known.items()),
            key=lambda p: -p["last"])

        results.append({
            "name": t["name"],
            "nation": (t.get("nation") or {}).get("name"),
            "mayor": (t.get("mayor") or {}).get("name"),
            "open": bool((t.get("status") or {}).get("isOpen")),
            "kind": kind,
            "outcome": outcome,
            "residents": len(res),
            "chunks": (t.get("stats") or {}).get("numTownBlocks") or 0,
            "balance": round((t.get("stats") or {}).get("balance") or 0),
            "players": players_info,
            "at": mayor_at,
            "hours_left": (mayor_at - now) / 3600000,
        })
    results.sort(key=lambda x: x["at"])

    print("Fetching nations...")
    nindex = call("GET", "/nations")
    nations = post_batches(
        "/nations",
        [n["uuid"] for n in nindex],
        {"name": True, "uuid": True, "king": True, "towns": True, "stats": True},
    )
    nation_results = []
    for n in nations:
        tl = [x["uuid"] for x in n.get("towns", []) if x["uuid"] not in ruined]
        if not tl:
            continue
        falls = [town_fall.get(u) for u in tl]
        # Any town with unknown data, or one that won't fall, keeps the nation alive.
        if any(f is None or f <= now for f in falls):
            continue
        at = max(falls)
        if at <= now + window:
            st = n.get("stats") or {}
            nation_results.append({
                "name": n["name"],
                "king": (n.get("king") or {}).get("name"),
                "towns": st.get("numTowns") or len(tl),
                "residents": st.get("numResidents") or 0,
                "chunks": st.get("numTownBlocks") or 0,
                "balance": round(st.get("balance") or 0),
                "at": at,
                "hours_left": (at - now) / 3600000,
            })
    nation_results.sort(key=lambda x: x["at"])

    print(f"Skipped {skipped} towns with missing/opted-out resident data.")
    return results, nation_results


def when(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%b %d %H:%M UTC")


def fmt(towns, nations, hours):
    towns = [r for r in towns if r["hours_left"] <= hours]
    nations = [r for r in nations if r["hours_left"] <= hours]
    lines = []
    if not towns:
        lines.append(f"No towns changing mayor or falling in the next {hours:g}h.")
    else:
        lines.append(f"Towns changing mayor or falling in the next {hours:g}h ({len(towns)}):")
        for r in towns:
            lines.append(
                f"- {r['name']} ({r['nation'] or 'no nation'}) | {'OPEN' if r['open'] else 'closed'} | "
                f"{r['outcome']} | mayor {r['mayor']} | {r['residents']} res, {r['chunks']} chunks, "
                f"{r['balance']}g | {r['hours_left']:.1f}h ({when(r['at'])})"
            )
    lines.append("")
    if not nations:
        lines.append(f"No nations falling in the next {hours:g}h.")
    else:
        lines.append(f"Nations falling in the next {hours:g}h ({len(nations)}):")
        for r in nations:
            lines.append(
                f"- {r['name']} | king {r['king']} | {r['towns']} towns, {r['residents']} res, "
                f"{r['chunks']} chunks, {r['balance']}g | {r['hours_left']:.1f}h ({when(r['at'])})"
            )
    return "\n".join(lines)


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="300">
<title>EarthMC towns and nations about to fall</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#111;color:#eee}
h1{font-size:1.2rem;margin:0 0 4px} p{margin:0 0 12px;color:#aaa;font-size:.85rem}
.tabs{display:flex;gap:8px;margin-bottom:12px}
.tab{flex:1;padding:10px;background:#222;color:#eee;border:1px solid #444;border-radius:6px;font-size:1rem}
.tab.on{background:#2a4;border-color:#2a4}
.f{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:12px}
.f label{font-size:.75rem;color:#9cf;display:flex;flex-direction:column;gap:3px}
.f div{display:flex;gap:4px}
input,select{width:100%;box-sizing:border-box;padding:6px;background:#222;color:#eee;border:1px solid #444;border-radius:4px}
.reset{padding:6px 12px;background:#444;border:0;color:#fff;border-radius:4px}
.w{overflow-x:auto} table{border-collapse:collapse;width:100%;min-width:820px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #333;font-size:.9rem}
th{color:#9cf;cursor:pointer;position:sticky;top:0;background:#111;white-space:nowrap}
.pin{background:none;border:0;color:#fc3;font-size:1.15rem;padding:0 4px}
tr.row{cursor:pointer}
.det td{background:#181818}
.dbox{font-size:.85rem;line-height:1.5;padding:4px 0}
.pt{min-width:0;width:auto;margin:6px 0}
.pt th,.pt td{padding:4px 12px 4px 0;font-size:.8rem;position:static;cursor:default}
small{color:#888}
</style></head><body>
<h1>EarthMC towns and nations about to fall</h1>
<p>Data from __UPDATED__ covering the next __HOURS__ hours (rerun the script to refresh). Changes happen at the new day, 13:00 GMT+3. <span id="count"></span></p>
<div class="tabs">
<button class="tab" data-t="towns">Towns</button>
<button class="tab" data-t="nations">Nations</button>
</div>
<div class="f">
<label>Search<input id="q" type="text" placeholder="name, mayor, king..."></label>
<label>Happening within
<select id="win">
<option value="6">6 hours</option><option value="12">12 hours</option>
<option value="24" selected>24 hours</option><option value="48">2 days</option>
<option value="72">3 days</option><option value="168">1 week</option>
<option value="336">2 weeks</option>
</select></label>
<label>Sort by
<select id="srt"><option value="">Custom (column header)</option>
<option value="at:1" selected>Time left (soonest)</option>
<option value="balance:-1">Gold in bank (high to low)</option>
<option value="residents:-1">Residents (high to low)</option>
<option value="chunks:-1">Chunks / claims (high to low)</option>
</select></label>
<label class="tw">Outcome
<select id="outc"><option value="">All</option><option value="falls">Falls</option><option value="transfer">Mayor changes</option></select></label>
<label class="tw">Open
<select id="opn"><option value="">Any</option><option value="yes">Open only</option><option value="no">Closed only</option></select></label>
<label class="tw">Watchlist
<select id="pinf"><option value="">All towns</option><option value="1">Pinned only</option></select></label>
<label>Gold in bank<div><input id="gmin" type="number" placeholder="min"><input id="gmax" type="number" placeholder="max"></div></label>
<label>Chunks<div><input id="cmin" type="number" placeholder="min"><input id="cmax" type="number" placeholder="max"></div></label>
<label>Residents<div><input id="rmin" type="number" placeholder="min"><input id="rmax" type="number" placeholder="max"></div></label>
<label class="nat">Towns in nation<div><input id="tmin" type="number" placeholder="min"><input id="tmax" type="number" placeholder="max"></div></label>
<label>&nbsp;<button class="reset" id="reset">Reset filters</button></label>
</div>
<div class="w"><table><thead><tr id="head"></tr></thead><tbody id="rows"></tbody></table></div>
<script>
const DATA = __DATA__;
const ids = ["q","win","srt","outc","opn","pinf","gmin","gmax","cmin","cmax","rmin","rmax","tmin","tmax"];
const COLS = {
  towns: [["pin","★"],["name","Town"],["open","Open"],["outcome","Outcome"],["balance","Gold"],["chunks","Chunks"],["residents","Res."],["at","When"],["nation","Nation"],["mayor","Mayor"]],
  nations: [["name","Nation"],["king","King"],["towns","Towns"],["residents","Residents"],["chunks","Chunks"],["balance","Gold"],["at","When"]]
};
const SEARCH = { towns: ["name","nation","mayor","outcome"], nations: ["name","king"] };
const $ = id => document.getElementById(id);
let tab = "towns";
const S = { towns: {k:"at", d:1}, nations: {k:"at", d:1} };
const expanded = new Set();
let pins = new Set();
try { pins = new Set(JSON.parse(localStorage.getItem("emcw") || "[]")); } catch (e) {}
const savePins = () => { try { localStorage.setItem("emcw", JSON.stringify([...pins])); } catch (e) {} };
try {
  const s = JSON.parse(localStorage.getItem("emcf3") || "{}");
  ids.forEach(i => { if (s[i] !== undefined) $(i).value = s[i]; });
  if (s.tab === "towns" || s.tab === "nations") tab = s.tab;
} catch (e) {}
if (!$("win").value) $("win").value = "24";
const num = id => { const v = $(id).value; return v === "" ? null : Number(v); };
const esc = s => String(s ?? "-").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const left = h => h <= 0 ? "any moment" : h < 48 ? h.toFixed(1) + "h" : Math.floor(h / 24) + "d " + Math.round(h % 24) + "h";
const ago = ms => { const d = ms / 86400000; return d < 1 ? Math.round(d * 24) + "h ago" : d.toFixed(1) + "d ago"; };

function details(r, now, n) {
  const rows = (r.players || []).map(p => {
    const h = (p.lapse - now) / 3600000;
    return `<tr><td>${p.next ? "★ " : ""}${esc(p.name)}</td><td>${esc(p.role || "resident")}</td><td>${ago(now - p.last)}</td><td>${h <= 0 ? "inactive" : left(h)}</td></tr>`;
  }).join("");
  return `<tr class="det"><td colspan="${n}"><div class="dbox"><b>${esc(r.name)}</b> | ${esc(r.nation)} | mayor ${esc(r.mayor)} | ${r.open ? "open" : "closed"} | ${r.balance}g | ${r.chunks} chunks<br>${esc(r.outcome)} on ${esc(new Date(r.at).toLocaleString())}` +
    `<table class="pt"><tr><th>Player</th><th>Role</th><th>Last online</th><th>Goes inactive in</th></tr>${rows}</table>` +
    `<small>★ = next mayor. A player counts as inactive after 42 days offline, checked at the new day.</small></div></td></tr>`;
}

function render() {
  try { const o = {tab}; ids.forEach(i => o[i] = $(i).value); localStorage.setItem("emcf3", JSON.stringify(o)); } catch (e) {}
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.t === tab));
  document.querySelectorAll(".nat").forEach(e => e.style.display = tab === "nations" ? "" : "none");
  document.querySelectorAll(".tw").forEach(e => e.style.display = tab === "towns" ? "" : "none");
  const now = Date.now(), q = $("q").value.toLowerCase(), win = Number($("win").value);
  const oc = $("outc").value, op = $("opn").value, pf = $("pinf").value;
  const rng = (v, lo, hi) => (lo === null || v >= lo) && (hi === null || v <= hi);
  const sv = $("srt").value;
  if (sv) { const kd = sv.split(":"); S[tab].k = kd[0]; S[tab].d = Number(kd[1]); }
  const cols = COLS[tab], s = S[tab], all = DATA[tab];
  const rows = all.filter(r =>
    (!q || SEARCH[tab].some(k => String(r[k] ?? "").toLowerCase().includes(q))) &&
    rng(r.balance, num("gmin"), num("gmax")) &&
    rng(r.chunks, num("cmin"), num("cmax")) &&
    rng(r.residents, num("rmin"), num("rmax")) &&
    (tab === "nations" || ((!oc || r.kind === oc) && (!op || (op === "yes") === r.open) && (!pf || pins.has(r.name)))) &&
    (tab === "towns" || rng(r.towns, num("tmin"), num("tmax"))) &&
    (r.at - now) / 3600000 <= win
  ).sort((a, b) => {
    if (tab === "towns") { const pa = pins.has(a.name), pb = pins.has(b.name); if (pa !== pb) return pa ? -1 : 1; }
    const x = a[s.k] ?? "", y = b[s.k] ?? "";
    return (x > y ? 1 : x < y ? -1 : 0) * s.d;
  });
  $("count").textContent = rows.length + " of " + all.length + " " + tab + " in data shown" + (tab === "towns" ? " (tap a town for details)" : "");
  $("head").innerHTML = cols.map(c => `<th data-k="${c[0]}">${c[1]}${s.k === c[0] ? (s.d > 0 ? " ▲" : " ▼") : ""}</th>`).join("");
  document.querySelectorAll("#head th").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if (k === "pin") return;
    const isNum = ["balance","residents","chunks","towns"].includes(k);
    s.d = (s.k === k) ? -s.d : (isNum ? -1 : 1); s.k = k; $("srt").value = ""; render();
  });
  $("rows").innerHTML = rows.map(r => {
    const cells = cols.map(c => {
      if (c[0] === "pin") return `<td><button class="pin" data-pin="${esc(r.name)}">${pins.has(r.name) ? "★" : "☆"}</button></td>`;
      if (c[0] === "at") return `<td>${left((r.at - now) / 3600000)} (${esc(new Date(r.at).toLocaleString())})</td>`;
      if (c[0] === "open") return `<td>${r.open ? "Yes" : "No"}</td>`;
      return `<td>${esc(r[c[0]])}</td>`;
    }).join("");
    const main = tab === "towns" ? `<tr class="row" data-n="${esc(r.name)}">${cells}</tr>` : `<tr>${cells}</tr>`;
    return main + (tab === "towns" && expanded.has(r.name) ? details(r, now, cols.length) : "");
  }).join("") || `<tr><td colspan="${cols.length}">Nothing matches.</td></tr>`;
}
ids.forEach(i => { $(i).addEventListener("input", render); $(i).addEventListener("change", render); });
$("reset").onclick = () => { ids.forEach(i => $(i).value = ""); $("win").value = "24"; $("srt").value = "at:1"; render(); };
document.querySelectorAll(".tab").forEach(b => b.onclick = () => { tab = b.dataset.t; render(); });
$("rows").addEventListener("click", ev => {
  const t = ev.target;
  const pin = t.closest ? t.closest(".pin") : null;
  if (pin) { const n = pin.dataset.pin; if (pins.has(n)) pins.delete(n); else pins.add(n); savePins(); render(); return; }
  const tr = t.closest ? t.closest("tr.row") : null;
  if (tr && tab === "towns") { const n = tr.dataset.n; if (expanded.has(n)) expanded.delete(n); else expanded.add(n); render(); }
});
render();
</script></body></html>"""


def write_html(path, towns, nations, hours):
    updated = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    data = json.dumps({"towns": towns, "nations": nations}).replace("</", "<\\/")
    page = (PAGE.replace("__HOURS__", f"{hours:g}")
                .replace("__UPDATED__",
