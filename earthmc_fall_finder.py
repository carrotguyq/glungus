#!/usr/bin/env python3
"""
EarthMC fall finder: towns and nations about to change hands or fall.

Rules this models (EarthMC, Nostra):
  * A mayor who hasn't logged in for 42 days loses the town at the next new day
    (10:00 UTC).
  * The town goes to the active councillor who has been in the town longest.
    Inactive councillors are skipped. With no active councillor it goes to the
    resident who has been in the town the longest.
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
import os
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.earthmc.net/v4"
INACTIVE_DAYS = 42
NEWDAY_HOUR = 10        # UTC hour of the daily new day
BATCH = 100             # lower this if the API rejects big batches
DELAY = 0.4             # ~150 req/min, under the 180/min limit

session = requests.Session()
session.headers["User-Agent"] = "earthmc-fall-finder/1.2"

# Since Nov 18 2026 the EarthMC API requires a key on every endpoint.
# Create one in game with /api key create, then provide it as the EMC_API_KEY
# environment variable (on GitHub: repo Settings > Secrets and variables > Actions).
API_KEY = os.environ.get("EMC_API_KEY", "").strip()
if API_KEY:
    session.headers["Authorization"] = f"Bearer {API_KEY}"
    session.headers["X-API-Key"] = API_KEY


def call(method, path, **kw):
    for attempt in range(6):
        r = session.request(method, API + path, timeout=30, **kw)
        if r.status_code == 429:  # rate limited, back off
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            hint = ("The key was sent but rejected, check it or create a new one with /api key create."
                    if API_KEY else
                    "EarthMC now requires an API key. Set the EMC_API_KEY environment variable "
                    "(on GitHub: Settings > Secrets and variables > Actions > EMC_API_KEY).")
            raise RuntimeError(f"HTTP {r.status_code} from {path}. {hint}")
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

    results, skipped = [], 0
    for t in towns:
        res = t.get("residents", [])
        mayor_id = (t.get("mayor") or {}).get("uuid")
        known = {r["uuid"]: last_seen[r["uuid"]] for r in res if r["uuid"] in last_seen}
        # Opted-out players are hidden by the API, so we can't be sure
        if not res or len(known) < len(res) or mayor_id not in known:
            skipped += 1
            continue

        mayor_at = next_newday(known[mayor_id] + inactive)   # mayor loses the town
        if mayor_at <= now:   # past the deadline but still mayor: probably Premium
            continue
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
        def longest(u):   # joined the town earliest; ties broken by most recent login
            return (joined.get(u, float("inf")), -known[u])
        active = [u for u, ts in known.items() if u != mayor_id and ts + inactive > mayor_at]
        active_c = [u for u in active if u in councillors]
        if active_c:
            succ = min(active_c, key=lambda u: joined.get(u, float("inf")))
            kind, outcome = "transfer", f"To {names[succ]} (councillor)"
        elif active:
            succ = min(active, key=longest)
            kind, outcome = "transfer", f"To {names[succ]} (longest in town)"
        else:
            succ = None
            kind, outcome = "falls", "Falls"

        top = min(active, key=longest) if active else None   # longest in the town
        top_a = max(active, key=lambda u: known[u]) if active else None   # most recent login
        succ_a = succ if active_c else top_a
        outcome_a = (outcome if active_c else
                     f"To {names[top_a]} (most active)" if top_a else "Falls")
        players_info = sorted(
            ({"name": names[u],
              "role": "mayor" if u == mayor_id else "councillor" if u in councillors else "",
              "last": ts,
              "lapse": next_newday(ts + inactive),
              "next": u == succ,
              "top": u == top,
              "next_a": u == succ_a,
              "top_a": u == top_a,
              "joined": joined.get(u)}
             for u, ts in known.items()),
            key=lambda p: -p["last"])

        results.append({
            "uuid": t["uuid"],
            "heir": names[top] if top else None,
            "heir_a": names[top_a] if top_a else None,
            "outcome_a": outcome_a,
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

    def spawn_of(obj):
        sp = (obj.get("coordinates") or {}).get("spawn") or {}
        if sp.get("x") is None or sp.get("z") is None:
            return None
        return {"x": round(sp["x"]), "y": round(sp.get("y") or 0), "z": round(sp["z"])}

    # fetched separately because a town's coordinates also hold every claimed chunk (large)
    if results:
        progress_spawn = post_batches("/towns", [r["uuid"] for r in results],
                                      {"uuid": True, "coordinates": True})
        spawns = {t["uuid"]: spawn_of(t) for t in progress_spawn if t.get("uuid")}
        for r in results:
            r["spawn"] = spawns.get(r["uuid"])

    print("Fetching nations...")
    nindex = call("GET", "/nations")
    nations = post_batches(
        "/nations",
        [n["uuid"] for n in nindex],
        {"name": True, "uuid": True, "king": True, "capital": True, "towns": True,
         "stats": True, "coordinates": True},
    )
    # Nations tab = nation capitals. The king is the capital's mayor, so when the capital's
    # mayor goes inactive the king changes (to the longest-standing resident) or the capital falls.
    by_town = {r["uuid"]: r for r in results}
    nation_results = []
    for n in nations:
        row = by_town.get((n.get("capital") or {}).get("uuid"))
        if not row:
            continue
        st = n.get("stats") or {}
        heir = row["heir"]
        heir_a = row["heir_a"]
        nation_results.append({
            "name": n["name"],
            "capital": row["name"],
            "king": (n.get("king") or {}).get("name") or row["mayor"],
            "open": row["open"],
            "kind": "transfer" if heir else "falls",
            "outcome": f"King becomes {heir}" if heir else "Capital falls",
            "heir": heir,
            "heir_a": heir_a,
            "outcome_a": f"King becomes {heir_a}" if heir_a else "Capital falls",
            "towns": st.get("numTowns") or len(n.get("towns", [])),
            "residents": st.get("numResidents") or 0,
            "chunks": st.get("numTownBlocks") or 0,
            "balance": round(st.get("balance") or 0),
            "capgold": row["balance"],
            "spawn": spawn_of(n),
            "at": row["at"],
            "hours_left": row["hours_left"],
            "town": row,
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
        lines.append(f"No nation capitals changing hands in the next {hours:g}h.")
    else:
        lines.append(f"Nation capitals changing hands in the next {hours:g}h ({len(nations)}):")
        for r in nations:
            lines.append(
                f"- {r['name']} (capital {r['capital']}) | {'OPEN' if r['open'] else 'closed'} | "
                f"{r['outcome']} | {r['towns']} towns, {r['residents']} res, {r['chunks']}ch, "
                f"{r['balance']}g | {r['hours_left']:.1f}h ({when(r['at'])})"
            )
    return "\n".join(lines)


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
.reset.on{background:#2a4}
.nd{color:#9cf;font-size:1rem;margin:0 0 8px}
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
.modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);align-items:flex-end;justify-content:center;z-index:10}
.card{background:#1a1a1a;width:100%;max-width:720px;max-height:88vh;overflow:auto;border-radius:12px 12px 0 0;padding:16px;box-sizing:border-box}
.card h2{margin:0 0 8px;font-size:1.15rem}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:.9rem;margin-bottom:12px}
.kv b{color:#9cf;font-weight:normal}
.btns{display:flex;gap:8px;margin:12px 0}
.btns button{flex:1;padding:9px;background:#333;color:#eee;border:1px solid #555;border-radius:6px;font-size:.9rem}
.card .pt{width:100%}
.card .pt th,.card .pt td{font-size:.85rem}
.hl td{background:#12301a}
</style></head><body>
<h1>EarthMC towns and nations about to fall</h1>
<p class="nd" id="nd"></p>
<p>Data from __UPDATED__ covering the next __HOURS__ hours (rerun the script to refresh). Changes happen at the new day (__NEWDAY__:00 UTC). <span id="count"></span></p>
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
<label>Outcome
<select id="outc"><option value="">All</option><option value="falls">Falls</option><option value="transfer">Mayor changes</option></select></label>
<label>Heir is
<select id="rule"><option value="">Longest in town</option><option value="active">Most active</option></select></label>
<label class="tw">Goes to
<select id="via"><option value="">Anyone</option><option value="councillor">Councillor</option><option value="resident">Resident (not councillor)</option></select></label>
<label>Open
<select id="opn"><option value="">Any</option><option value="yes">Open only</option><option value="no">Closed only</option></select></label>
<label class="tw">Watchlist
<select id="pinf"><option value="">All towns</option><option value="1">Pinned only</option></select></label>
<label>Gold in bank<div><input id="gmin" type="number" placeholder="min"><input id="gmax" type="number" placeholder="max"></div></label>
<label>Chunks<div><input id="cmin" type="number" placeholder="min"><input id="cmax" type="number" placeholder="max"></div></label>
<label>Residents<div><input id="rmin" type="number" placeholder="min"><input id="rmax" type="number" placeholder="max"></div></label>
<label class="nat">Towns in nation<div><input id="tmin" type="number" placeholder="min"><input id="tmax" type="number" placeholder="max"></div></label>
<label>&nbsp;<button class="reset" id="multi">Multi-sort: off</button></label>
<label>&nbsp;<button class="reset" id="reset">Reset filters</button></label>
</div>
<div class="w"><table><thead><tr id="head"></tr></thead><tbody id="rows"></tbody></table></div>
<div class="modal" id="modal"><div class="card" id="card"></div></div>
<script>
const DATA = __DATA__;
const NEWDAY = __NEWDAY__;
const nextNewDay = () => { const n = new Date(); let t = Date.UTC(n.getUTCFullYear(), n.getUTCMonth(), n.getUTCDate(), NEWDAY); if (t <= n.getTime()) t += 86400000; return t; };
const tickND = () => {
  const ms = nextNewDay() - Date.now();
  $("nd").textContent = "Next new day in " + Math.floor(ms / 3600000) + "h " + Math.floor(ms % 3600000 / 60000) + "m (" + String(NEWDAY).padStart(2, "0") + ":00 UTC)";
};
const ids = ["q","win","srt","outc","rule","via","opn","pinf","gmin","gmax","cmin","cmax","rmin","rmax","tmin","tmax"];
const COLS = {
  towns: [["pin","★"],["name","Town"],["open","Open"],["outcome","Outcome"],["balance","Gold"],["chunks","Chunks"],["residents","Res."],["at","When"],["nation","Nation"],["mayor","Mayor"]],
  nations: [["name","Nation"],["capital","Capital"],["king","King"],["open","Open"],["outcome","Outcome"],["towns","Towns"],["residents","Res."],["chunks","Chunks"],["balance","Gold"],["capgold","Cap. gold"],["at","When"]]
};
const SEARCH = { towns: ["name","nation","mayor","outcome"], nations: ["name","capital","king","heir"] };
const $ = id => document.getElementById(id);
let tab = "towns", multi = false;
const S = { towns: [{k:"at", d:1}], nations: [{k:"at", d:1}] };
let openRow = null, openNr = null;
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

function openTown(r, nr) {
  openRow = r; openNr = nr || null;
  const now = Date.now();
  const act = $("rule").value === "active";
  const star = p => nr ? (act ? p.top_a : p.top) : (act ? p.next_a : p.next);
  const outTxt = act ? (nr ? nr.outcome_a : r.outcome_a) : (nr ? nr.outcome : r.outcome);
  const fmtSp = sp => sp ? sp.x + ", " + sp.y + ", " + sp.z : "unknown";
  const dur = ms => { const d = ms / 86400000; return d < 1 ? Math.round(d * 24) + "h" : d < 10 ? d.toFixed(1) + "d" : Math.round(d) + "d"; };
  const ps = r.players || [];
  const winner = ps.find(star);
  let winTxt = "", leadTxt = "";
  if (winner) {
    const pool = ps.filter(p => p.role !== "mayor" && p.lapse > r.at);   // still active at the new day
    const viaCouncillor = !nr && winner.role === "councillor";
    const cand = viaCouncillor ? pool.filter(p => p.role === "councillor") : pool;
    const longest = !act || viaCouncillor;
    const sorted = [...cand].sort(longest
      ? (a, b) => (a.joined ?? Infinity) - (b.joined ?? Infinity)
      : (a, b) => b.last - a.last);
    const second = sorted.find(p => p !== winner);
    winTxt = esc(winner.name) + (winner.joined ? " (in town for " + dur(now - winner.joined) + ")" : "");
    if (!second) leadTxt = "only active candidate";
    else if (longest && winner.joined && second.joined) leadTxt = dur(second.joined - winner.joined) + " longer in town than " + esc(second.name);
    else if (!longest) leadTxt = "logged in " + dur(winner.last - second.last) + " more recently than " + esc(second.name);
  }
  const rows = ps.map(p => {
    const h = (p.lapse - now) / 3600000;
    return `<tr class="${star(p) ? "hl" : ""}"><td>${star(p) ? "★ " : ""}${esc(p.name)}</td><td>${esc(p.role || "resident")}</td><td>${ago(now - p.last)}</td><td>${p.joined ? dur(now - p.joined) : "-"}</td><td>${h <= 0 ? "inactive" : left(h)}</td></tr>`;
  }).join("");
  $("card").innerHTML =
    `<h2>${esc(r.name)}</h2>` + (nr ? `<p>Capital of ${esc(nr.name)}. The king is the capital's mayor.</p>` : "") +
    `<div class="kv"><b>Outcome</b><span>${esc(outTxt ?? "")}</span>` +
    (winTxt ? `<b>Winner</b><span>${winTxt}</span>` : "") + (leadTxt ? `<b>Lead</b><span>${leadTxt}</span>` : "") +
    `<b>When</b><span>${left((r.at - now) / 3600000)} (${esc(new Date(r.at).toLocaleString())})</span>` +
    `<b>Status</b><span>${r.open ? "Open" : "Closed"}</span>` +
    (nr ? `<b>Nation spawn</b><span>${fmtSp(nr.spawn)}</span><b>Capital spawn</b><span>${fmtSp(r.spawn)}</span>` : `<b>Spawn</b><span>${fmtSp(r.spawn)}</span>`) +
    `<b>Gold</b><span>${r.balance}</span><b>Chunks</b><span>${r.chunks}</span>` +
    `<b>Residents</b><span>${r.residents}</span><b>Nation</b><span>${esc(r.nation)}</span><b>Mayor</b><span>${esc(r.mayor)}</span></div>` +
    `<div class="btns"><button data-act="pin">${pins.has(r.name) ? "★ Unwatch" : "☆ Watch"}</button>` +
    (r.open ? `<button data-act="copy">Copy /t join ${esc(r.name)}</button>` : "") +
    `<button data-act="close">Close</button></div>` +
    `<table class="pt"><tr><th>Player</th><th>Role</th><th>Last online</th><th>In town for</th><th>Goes inactive in</th></tr>${rows}</table>` +
    `<small>★ = ${nr ? (act ? "heir (most active)" : "heir (longest in the town)") : "next mayor"}. A player counts as inactive after 42 days offline, checked at the new day. Playtime is not in the API, so "In town for" is used instead.</small>`;
  $("modal").style.display = "flex";
}
function closeTown() { openRow = null; openNr = null; $("modal").style.display = "none"; }

function render() {
  try { const o = {tab}; ids.forEach(i => o[i] = $(i).value); localStorage.setItem("emcf3", JSON.stringify(o)); } catch (e) {}
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("on", b.dataset.t === tab));
  document.querySelectorAll(".nat").forEach(e => e.style.display = tab === "nations" ? "" : "none");
  document.querySelectorAll(".tw").forEach(e => e.style.display = tab === "towns" ? "" : "none");
  const now = Date.now(), q = $("q").value.toLowerCase(), win = Number($("win").value);
  const oc = $("outc").value, op = $("opn").value, pf = $("pinf").value, via = $("via").value;
  const rng = (v, lo, hi) => (lo === null || v >= lo) && (hi === null || v <= hi);
  const sv = $("srt").value;
  if (sv) { const kd = sv.split(":"); S[tab] = [{k: kd[0], d: Number(kd[1])}]; }
  const rule = $("rule").value;
  const cols = COLS[tab], list = S[tab];
  const all = DATA[tab].map(r => rule === "active" ? Object.assign({}, r, {outcome: r.outcome_a ?? r.outcome, heir: r.heir_a ?? r.heir}) : r);
  const rows = all.filter(r =>
    (!q || SEARCH[tab].some(k => String(r[k] ?? "").toLowerCase().includes(q))) &&
    rng(r.balance, num("gmin"), num("gmax")) &&
    rng(r.chunks, num("cmin"), num("cmax")) &&
    rng(r.residents, num("rmin"), num("rmax")) &&
    (!oc || r.kind === oc) && (!op || (op === "yes") === r.open) &&
    (tab === "nations" || ((!pf || pins.has(r.name)) &&
      (!via || (via === "councillor") === String(r.outcome).includes("(councillor)") && (via === "councillor" || r.kind === "transfer")))) &&
    (tab === "towns" || rng(r.towns, num("tmin"), num("tmax"))) &&
    (r.at - now) / 3600000 <= win
  ).sort((a, b) => {
    if (tab === "towns") { const pa = pins.has(a.name), pb = pins.has(b.name); if (pa !== pb) return pa ? -1 : 1; }
    for (const t of list) {
      const x = a[t.k] ?? "", y = b[t.k] ?? "";
      if (x !== y) return (x > y ? 1 : -1) * t.d;
    }
    return 0;
  });
  $("count").textContent = rows.length + " of " + all.length + " " + tab + " in data shown" + " (tap a row for details)";
  $("head").innerHTML = cols.map(c => {
    const i = list.findIndex(x => x.k === c[0]);
    const mark = i < 0 ? "" : (list[i].d > 0 ? " ▲" : " ▼") + (list.length > 1 ? (i + 1) : "");
    return `<th data-k="${c[0]}">${c[1]}${mark}</th>`;
  }).join("");
  document.querySelectorAll("#head th").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if (k === "pin") return;
    const def = ["balance","capgold","residents","chunks","towns"].includes(k) ? -1 : 1;
    const i = list.findIndex(x => x.k === k);
    if (!multi) {
      S[tab] = (list.length === 1 && i === 0) ? [{k, d: -list[0].d}] : [{k, d: def}];
    } else if (i < 0) {
      list.push({k, d: def});
    } else if (list[i].d === def) {
      list[i].d = -def;
    } else {
      list.splice(i, 1);
      if (!list.length) list.push({k: "at", d: 1});
    }
    $("srt").value = ""; render();
  });
  $("rows").innerHTML = rows.map(r => {
    const cells = cols.map(c => {
      if (c[0] === "pin") return `<td><button class="pin" data-pin="${esc(r.name)}">${pins.has(r.name) ? "★" : "☆"}</button></td>`;
      if (c[0] === "at") return `<td>${left((r.at - now) / 3600000)} (${esc(new Date(r.at).toLocaleString())})</td>`;
      if (c[0] === "open") return `<td>${r.open ? "Yes" : "No"}</td>`;
      return `<td>${esc(r[c[0]])}</td>`;
    }).join("");
    const main = `<tr class="row" data-n="${esc(r.name)}">${cells}</tr>`;
    return main;
  }).join("") || `<tr><td colspan="${cols.length}">Nothing matches.</td></tr>`;
}
ids.forEach(i => { $(i).addEventListener("input", render); $(i).addEventListener("change", render); });
$("multi").onclick = () => {
  multi = !multi;
  $("multi").textContent = "Multi-sort: " + (multi ? "on" : "off");
  $("multi").classList.toggle("on", multi);
};
$("reset").onclick = () => { ids.forEach(i => $(i).value = ""); $("win").value = "24"; $("srt").value = "at:1"; render(); };
document.querySelectorAll(".tab").forEach(b => b.onclick = () => { tab = b.dataset.t; render(); });
$("rows").addEventListener("click", ev => {
  const t = ev.target;
  const pin = t.closest ? t.closest(".pin") : null;
  if (pin) { const n = pin.dataset.pin; if (pins.has(n)) pins.delete(n); else pins.add(n); savePins(); render(); return; }
  const tr = t.closest ? t.closest("tr.row") : null;
  if (tr) { const r = DATA[tab].find(x => x.name === tr.dataset.n); if (r) { if (tab === "towns") openTown(r); else openTown(r.town, r); } }
});
$("card").addEventListener("click", ev => {
  const b = ev.target.closest ? ev.target.closest("button") : null;
  if (!b || !openRow) return;
  if (b.dataset.act === "pin") {
    const n = openRow.name;
    if (pins.has(n)) pins.delete(n); else pins.add(n);
    savePins(); render(); openTown(openRow, openNr);
  } else if (b.dataset.act === "copy") {
    try { navigator.clipboard.writeText("/t join " + openRow.name); b.textContent = "Copied"; } catch (e) { b.textContent = "Copy failed"; }
  } else closeTown();
});
$("modal").addEventListener("click", ev => { if (ev.target === $("modal")) closeTown(); });
setInterval(() => { if (!openRow) location.reload(); }, 300000);
tickND(); setInterval(tickND, 30000);
render();
</script></body></html>"""


def write_html(path, towns, nations, hours):
    updated = datetime.now(timezone.utc).strftime("%b %d, %H:%M UTC")
    data = json.dumps({"towns": towns, "nations": nations}).replace("</", "<\\/")
    page = (PAGE.replace("__NEWDAY__", str(NEWDAY_HOUR))
                .replace("__HOURS__", f"{hours:g}")
                .replace("__UPDATED__", updated)
                .replace("__DATA__", data))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)


def send_webhook(url, text):
    # Discord caps messages at 2000 chars
    for i in range(0, len(text), 1900):
        requests.post(url, json={"content": text[i:i + 1900]}, timeout=15)
        time.sleep(1)


def main():
    global NEWDAY_HOUR
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24, help="window for terminal/webhook output")
    ap.add_argument("--html", help="Write results to this HTML file (open it in a browser)")
    ap.add_argument("--html-hours", type=float, default=336,
                    help="how far ahead the web page looks (default 336 = 2 weeks)")
    ap.add_argument("--newday-hour", type=int, default=NEWDAY_HOUR,
                    help="UTC hour of the daily new day (default 10)")
    ap.add_argument("--webhook", help="Discord webhook URL to post results to")
    ap.add_argument("--loop", type=int, help="Re-run every N minutes")
    args = ap.parse_args()
    NEWDAY_HOUR = args.newday_hour

    while True:
        reach = max(args.hours, args.html_hours if args.html else 0)
        towns, nations = find_falling(reach)
        text = fmt(towns, nations, args.hours)
        print(text)
        if args.html:
            write_html(args.html, towns, nations, args.html_hours)
            print(f"Wrote {args.html}")
        if args.webhook:
            send_webhook(args.webhook, text)
        if not args.loop:
            break
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()
