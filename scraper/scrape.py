"""Fetch Blackeberg P11 Röd data from Profixio and Cup Manager into data.json.

Run by the GitHub Action in .github/workflows/update.yml. The script decides by itself
whether there is anything to do right now (see decide()), so the Action can wake it every hour.

  python scraper/scrape.py            # let the schedule decide
  python scraper/scrape.py --force    # do a full update now
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Europe/Stockholm")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data.json")
UA = "BlackebergP11-app/1.0 (private team-following app; github.com/keithhearne/bbpered)"

# Competitions on Profixio. teamWebId is the team id in the URL and in the match log.
SERIES = {
    "serie": {"url": "https://www.profixio.com/app/leagueid27806/teams/1586476?k=1184305", "teamWebId": 1586476},
    "usm": {"url": "https://www.profixio.com/app/leagueid27658/teams/1625380?k=1186504", "teamWebId": 1625380},
}
OUR = "KFUM Blackebergs IK Röd"
# Cups on Cup Manager (add new cups here when they are announced).
CUPS = [
    {"name": "Södertälje Open", "host": "https://sodertaljeopen.cupmanager.net", "edition": "2026,sv",
     "tournamentId": 74326889, "categoryId": 74326938, "ourName": "KFUM Blackebergs IK P11 Röd"},
]

LIVE_BEFORE = timedelta(minutes=10)     # start polling this long before tip-off
LIVE_AFTER = timedelta(minutes=100)     # ...and keep going this long after tip-off
LIVE_STEP = 600                         # seconds between live updates
AFTER_GAME = timedelta(hours=6)         # hourly catch-up checks after a game
NIGHTLY_HOUR = 3                        # full fixture check every night (Swedish time)
WEEKEND_CHECK_HOURS = (9, 14)           # extra light check on Fri–Sun

S = requests.Session()
S.headers["User-Agent"] = UA


def get(url, **kw):
    for attempt in range(3):
        try:
            r = S.get(url, timeout=40, **kw)
            r.raise_for_status()
            time.sleep(1.5)  # be gentle with the sources
            return r.text
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed ({attempt + 1}/3): {url} {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"could not fetch {url}")


def now():
    return datetime.now(TZ)


def log(*a):
    print(now().strftime("%H:%M:%S"), *a, flush=True)


# ---------- Profixio ----------

def js_json(raw):
    """Decode the argument of JSON.parse('...') as found in the page HTML."""
    s = html.unescape(raw)
    for _ in range(4):
        try:
            v = json.loads(s)
        except json.JSONDecodeError:
            try:
                s = json.loads('"' + s + '"')
                continue
            except json.JSONDecodeError:
                return None
        if isinstance(v, str):
            s = v
            continue
        return v
    return None


def grab(page, key):
    m = re.search(key + r":\s*JSON\.parse\(&#039;(.*?)&#039;\)", page, re.S)
    return js_json(m.group(1)) if m else None


def clean(t):
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return html.unescape(re.sub(r"\s+", " ", t)).strip()


def parse_matches(page):
    out = []
    blocks = re.split(r'<li[^>]*wire:key="listkamp_', page)[1:]
    for b in blocks:
        mid = re.match(r"(\d+)", b).group(1)
        b = b.split("</li>")[0] if "</li>" in b else b
        xd = re.search(r"homegoals: '(\d*)',\s*awaygoals: '(\d*)',\s*hasLivescore: (true|false),\s*hasResult: (true|false),.*?timestamp: (\d+)", b, re.S)
        if not xd:
            continue
        hg, ag, live, res, ts = xd.groups()
        names = [clean(x) for x in re.findall(r'class="leading-5 max-w-60[^"]*">(.*?)</div>', b, re.S)]
        if len(names) < 2:
            continue
        venue = re.search(r'<div class="text-xs text-right">([^<]*)</div>', b)
        day = datetime.fromtimestamp(int(ts), TZ)  # the timestamp is the match day; the time is printed in the card
        tm = re.search(r"<div>\s*(\d{1,2}:\d{2})\s*</div>", b)
        out.append({
            "id": mid, "date": day.strftime("%Y-%m-%d"), "time": tm.group(1).zfill(5) if tm else "",
            "home": names[0], "away": names[1], "venue": html.unescape(venue.group(1)).strip() if venue else "",
            "hs": int(hg) if hg != "" else None, "as": int(ag) if ag != "" else None,
            "result": res == "true", "live": live == "true", "log": "Matchlogg" in b,
        })
    return out


def parse_table(page):
    rows, logos = [], {}
    for tr in re.findall(r'<tr[^>]*wire:key="grp_[^"]*"[^>]*>(.*?)</tr>', page, re.S):
        cells = [clean(td) for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 10:
            continue
        logo = re.search(r"/logos/gklubb/([\w]+)", tr)
        nm = re.search(r'<div class="flex flex-col justify-center">(.*?)</div>', tr, re.S)
        name = clean(nm.group(1)) if nm else cells[1]
        if logo:
            logos[name] = logo.group(1)
        num = lambda x: int(re.sub(r"[^\d-]", "", x) or 0)  # noqa: E731
        # pos, team, GP, W, L, (Vinst%), FOR, AGST, +/-, L5
        rows.append([num(cells[0]), name, num(cells[2]), num(cells[3]), num(cells[4]),
                     num(cells[6]), num(cells[7]), num(cells[8]), cells[9].replace(" ", "")])
    return rows, logos


def parse_match_log(page, team_web_id):
    events = grab(page, "events")
    lineup = grab(page, "lineup")
    hw = re.search(r"homewebid: (\d+)", page)
    aw = re.search(r"awaywebid: (\d+)", page)
    if events is None or not hw or not aw:
        return None, None
    hw, aw = int(hw.group(1)), int(aw.group(1))
    names, rows = {}, []
    for x in reversed(events):
        tid = x.get("teamId")
        s = "h" if tid == hw else "a" if tid == aw else "-"
        if x.get("goals"):
            code = str(x["goals"])
        elif x.get("isPersonFoul"):
            code = "F"
        elif x.get("timeout"):
            code = "T"
        elif x.get("startsPeriod") or x.get("startsMatch"):
            code = "S"
        elif x.get("stopsMatch"):
            code = "X"
        elif x.get("stopsPeriod"):
            code = "E"
        else:
            code = "?" + re.sub(r"[ ,;]", "_", x.get("description") or "")
        if code in ("?Periodpaus", "?Halvtidspaus"):
            code = "E"
        p = x.get("person")
        num = (p.get("number") or "0") if p else ""
        if p:
            names[s + num] = p.get("name")
        cs = x.get("currentScore") or {}
        sc = lambda v: "" if v is None else str(v)  # noqa: E731
        rows.append(",".join([sc(x.get("period")), s, code, num, sc(cs.get("home")) if cs else "",
                              sc(cs.get("away")) if cs else "", (x.get("startedAt") or "")[11:16].replace(":", "")]))
    pbp = {"names": names, "ev": ";".join(rows)}
    lu = None
    if lineup:
        our_side = "h" if hw == team_web_id else "a"
        pts = {}
        for r in rows:
            per, side, code, num = r.split(",")[:4]
            if side == our_side and code in ("1", "2", "3"):
                pts[num] = pts.get(num, 0) + int(code)
        lu = []
        for p in lineup:
            if p.get("webTeamId") != team_web_id or p.get("type") != "player":
                continue
            n = str(p.get("number") or "")
            lu.append([n, p.get("name"), 1 if p.get("isCaptain") else 0, p.get("foulCount") or 0, pts.get(n, 0)])
        lu.sort(key=lambda r: int(r[0]) if r[0].isdigit() else 999)
    return pbp, lu


def scrape_profixio(prev, live_only=False):
    out = {"series": {}, "pbp": {}, "lineups": {}, "logos": {}, "live": {}}
    prev_series = prev.get("series", {})
    for key, cfg in SERIES.items():
        log("Profixio", key)
        try:
            page = get(cfg["url"])
            matches = parse_matches(page)
        except Exception as e:  # noqa: BLE001
            log("  matches failed:", e)
            out["series"][key] = prev_series.get(key, {})
            continue
        if not matches:
            log("  no matches found — keeping previous data")
            out["series"][key] = prev_series.get(key, {})
            continue
        table = prev_series.get(key, {}).get("table", [])
        if not live_only:
            try:
                table, logos = parse_table(get(cfg["url"] + "&tab=table"))
                out["logos"].update(logos)
            except Exception as e:  # noqa: BLE001
                log("  table failed:", e)
        rows = []
        for m in matches:
            row = [m["date"], m["time"], m["home"], m["away"], m["venue"]]
            if m["result"] and m["hs"] is not None and not m["live"]:
                row += [m["hs"], m["as"], m["id"]]
            elif m["live"] and m["hs"] is not None:
                out["live"][m["id"]] = [m["hs"], m["as"]]
                row += [None, None, m["id"]]
            else:
                row += [None, None, m["id"]]
            rows.append(row)
            # Match log: fetch for live games and for finished games we don't have yet (or that just finished).
            have = m["id"] in prev.get("pbp", {}) and m["id"] in prev.get("final", [])
            want = (m["live"] or (m["result"] and not have)) and (m["log"] or m["live"] or m["result"])
            if live_only and not m["live"] and not (m["result"] and not have):
                want = False
            if want:
                try:
                    log("  match log", m["id"])
                    pbp, lu = parse_match_log(get(cfg["url"] + "&expandmatch=" + m["id"]), cfg["teamWebId"])
                    if pbp and pbp["ev"]:
                        out["pbp"][m["id"]] = pbp
                    if lu:
                        out["lineups"][m["id"]] = lu
                except Exception as e:  # noqa: BLE001
                    log("  match log failed:", e)
        out["series"][key] = {"matches": rows, "table": table}
    return out


# ---------- Cup Manager ----------

def scrape_cups(prev):
    games, live = [], {}
    for cup in CUPS:
        log("Cup Manager", cup["name"])
        M = "{...+on+Match:{arena:{},away:{},home:{},result:{}}}"
        q = (f"Category({{categoryId:{cup['categoryId']},tournamentId:{cup['tournamentId']}}})"
             f"{{stages:[{{divisions:[{{...+on+Conference:{{matches:[{M}]}},...+on+Playoff:{{rounds:[{{matches:[{M}]}}]}}}}]}}]}}")
        enc = re.sub(r"[{}():,\[\]]", lambda c: quote(c.group(0)), q)
        try:
            R = json.loads(get(f"{cup['host']}/rest/results_api/call?call={enc}&lang=sv&tournamentId={cup['tournamentId']}"))["responses"]
        except Exception as e:  # noqa: BLE001
            log("  cup failed:", e)
            games += [g for g in prev.get("cupGames", []) if len(g) > 6 and g[6] == cup["name"]]
            continue
        ent = lambda k: (R.get(k) or {}).get("entity") or {}  # noqa: E731

        def name(ref):
            n = ent(ref).get("name") or {}
            n = n.get("sv") or n.get("en") or next(iter(n.values()), "") if isinstance(n, dict) else str(n)
            return OUR if n == cup["ourName"] else n

        for k, v in R.items():
            if not re.fullmatch(r"Match\(\{id:\d+\}\)", k):
                continue
            m = v.get("entity") or {}
            h, a = name((m.get("home") or {}).get("href")), name((m.get("away") or {}).get("href"))
            if OUR not in (h, a) or not m.get("start"):
                continue
            kick = datetime.fromtimestamp(m["start"] / 1000, TZ)
            res = ent((m.get("result") or {}).get("href"))
            arena = ent((m.get("arena") or {}).get("href")).get("completeName") or ""
            url = f"{cup['host']}/{cup['edition']}/result/match/{m['id']}"
            row = [kick.strftime("%Y-%m-%d"), kick.strftime("%H:%M"), h, a, arena, url, cup["name"], None, None, str(m["id"])]
            if res.get("finished") or m.get("finished"):
                row[7], row[8] = res.get("homeGoals", res.get("homePoints")), res.get("awayGoals", res.get("awayPoints"))
            elif res.get("live") or m.get("live"):
                live[str(m["id"])] = [res.get("homeGoals", 0), res.get("awayGoals", 0)]
            games.append(row)
    games.sort(key=lambda g: g[0] + g[1])
    return games, live


# ---------- schedule ----------

def fixtures(prev):
    """All our games we know about, as (kick-off, id, finished)."""
    out = []
    for key, s in prev.get("series", {}).items():
        for r in s.get("matches", []):
            if r[1]:
                out.append((datetime.fromisoformat(f"{r[0]}T{r[1]}").replace(tzinfo=TZ), r[7] if len(r) > 7 else None, r[5] is not None))
    for g in prev.get("cupGames", []):
        out.append((datetime.fromisoformat(f"{g[0]}T{g[1]}").replace(tzinfo=TZ), g[9] if len(g) > 9 else None, len(g) > 7 and g[7] is not None))
    return out


def decide(prev, t):
    """Return ('live', end_time) / ('full', None) / ('skip', reason)."""
    games = fixtures(prev)
    soon = [k for k, _, done in games if not done and k - LIVE_BEFORE - timedelta(minutes=65) <= t <= k + LIVE_AFTER]
    if soon:
        return "live", max(soon) + LIVE_AFTER
    recent = [k for k, _, _ in games if k + LIVE_AFTER < t <= k + AFTER_GAME]
    if recent:
        return "full", "catch-up after a game"
    if t.hour == NIGHTLY_HOUR:
        return "full", "nightly check"
    if t.weekday() >= 4 and t.hour in WEEKEND_CHECK_HOURS:
        return "full", "weekend check"
    if not prev:
        return "full", "first run"
    return "skip", "nothing scheduled"


def build(prev, live_only=False):
    p = scrape_profixio(prev, live_only)
    cups, cup_live = scrape_cups(prev)
    data = {
        "updated": now().strftime("%Y-%m-%dT%H:%M"),
        "series": p["series"],
        "cupGames": cups or prev.get("cupGames", []),
        "pbp": {**prev.get("pbp", {}), **p["pbp"]},
        "lineups": {**prev.get("lineups", {}), **p["lineups"]},
        "logos": {**prev.get("logos", {}), **p["logos"]},
        "live": {**p["live"], **cup_live},
    }
    data["final"] = sorted({r[7] for s in data["series"].values() for r in s.get("matches", []) if len(r) > 7 and r[5] is not None and r[7]})
    return data


def save(data):
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("saved data.json", os.path.getsize(OUT), "bytes;", len(data["live"]), "live game(s)")


def commit(msg):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    os.system('git config user.name "p11-bot" && git config user.email "actions@users.noreply.github.com"')
    os.system("git add data.json")
    if os.system("git diff --cached --quiet") != 0:
        os.system(f'git commit -q -m "{msg}" && (git push -q || (git pull -q --rebase && git push -q))')
        log("pushed")


def main():
    prev = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
    force = "--force" in sys.argv or os.environ.get("FORCE") == "1"
    t = now()
    mode, info = ("full", "manual run") if force else decide(prev, t)
    log("mode:", mode, info if not isinstance(info, datetime) else "until " + info.strftime("%H:%M"))
    if mode == "skip":
        return
    if mode == "full":
        save(build(prev))
        commit("Update data")
        return
    # Live mode: wait for tip-off, then update every 10 minutes until the game window closes.
    end = info
    while True:
        t = now()
        games = [(k, done) for k, _, done in fixtures(prev) if not done and k <= end]
        active = [k for k, _ in games if k - LIVE_BEFORE <= t <= k + LIVE_AFTER]
        nxt = min((k for k, _ in games if k - LIVE_BEFORE > t), default=None)
        if not active and nxt:
            wait = (nxt - LIVE_BEFORE - t).total_seconds()
            log(f"waiting {int(wait // 60)} min for tip-off at {nxt.strftime('%H:%M')}")
            time.sleep(wait)
            continue
        data = build(prev, live_only=True)
        save(data)
        commit("Live update")
        prev = data
        end = max([end] + [k + LIVE_AFTER for k, _, done in fixtures(prev) if not done and k <= now() + LIVE_BEFORE])
        if now() + timedelta(seconds=LIVE_STEP) > end:
            break
        time.sleep(LIVE_STEP)
    save(build(prev))  # full update (tables too) when the window closes
    commit("Update data after game")


if __name__ == "__main__":
    main()
