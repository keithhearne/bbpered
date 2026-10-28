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
STATUS_JSON = os.path.join(ROOT, "status.json")
STATUS_MD = os.path.join(ROOT, "STATUS.md")
UA = "BlackebergP11-app/1.0 (private team-following app; github.com/keithhearne/bbpered)"

# Competitions on Profixio. teamWebId is the team id in the URL and in the match log.
SERIES = {
    "serie": {"url": "https://www.profixio.com/app/leagueid27806/teams/1586476?k=1184305", "teamWebId": 1586476, "team": "KFUM Blackebergs IK Röd"},
    "usm": {"url": "https://www.profixio.com/app/leagueid27658/teams/1625380?k=1186504", "teamWebId": 1625380, "team": "KFUM Blackebergs IK"},
}
OUR = "KFUM Blackebergs IK Röd"
# The club's list of teams in every competition this season. Checked nightly for new competitions
# (e.g. a new Ungdoms-SM round or a play-off series) that P11 Röd has been entered into.
# The list is filtered on "Pojkar U16" (the page's own search box, t=…), so only ~5 of the club's ~73 teams are read.
REG_URL = "https://www.profixio.com/app/league/registrations/25232?sn=776&t=Pojkar%20U16"


def addq(url, param):
    return url + ("&" if "?" in url else "?") + param
# Cups on Cup Manager (add new cups here when they are announced).
CUPS = [
    {"name": "Södertälje Open", "host": "https://sodertaljeopen.cupmanager.net", "edition": "2026,sv",
     "tournamentId": 74326889, "categoryId": 74326938, "ourName": "KFUM Blackebergs IK P11 Röd"},
]

LIVE_BEFORE = timedelta(minutes=10)     # start polling this long before tip-off
LIVE_AFTER = timedelta(minutes=100)     # ...and keep going this long after tip-off
LIVE_STEP = 120                         # seconds between live checks (score goes to the "live" branch, no site rebuild)
MAIN_EVERY = timedelta(minutes=15)      # during a game, the full data.json is published at most this often (plus when a game ends)
LIVE_BRANCH = "live"                    # branch that holds live.json (not the website branch, so no GitHub Pages rebuild)
AFTER_GAME = timedelta(hours=6)         # hourly catch-up checks after a game
NIGHT_START, NIGHT_END = 3, 9           # nightly full check: the first run after 03:00 each day (Swedish time)
WEEKEND_EVERY = timedelta(hours=5)      # Fri–Sun daytime: a check at least every 5 hours
LIVE_LEAD = timedelta(hours=3)          # a run this close before tip-off stays on and waits for the game

S = requests.Session()
S.headers["User-Agent"] = UA
S.headers["Accept-Language"] = "sv-SE,sv;q=0.9"


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
                     num(cells[6]), num(cells[7]), num(cells[8]), cells[9].replace(" ", "").replace("W", "V").replace("L", "F")])
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
    out["comps"] = {}
    queue = list(all_series(prev).items())
    while queue:
        key, cfg = queue.pop(0)
        log("Profixio", key)
        try:
            page = get(cfg["url"])
            if not live_only:
                for nk, nc in find_rounds(page, cfg, [c for _, c in queue] + list(out["comps"].values()), prev).items():
                    out["comps"][nk] = nc
                    queue.append((nk, nc))
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
                table, logos = parse_table(get(addq(cfg["url"], "tab=table")))
                out["logos"].update(logos)
            except Exception as e:  # noqa: BLE001
                log("  table failed:", e)
        rows = []
        old_venue = {r[7]: r[4] for r in prev_series.get(key, {}).get("matches", []) if len(r) > 7 and r[4]}
        for m in matches:
            if not m["venue"]:
                m["venue"] = old_venue.get(m["id"], "")
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
                    pbp, lu = parse_match_log(get(addq(cfg["url"], "expandmatch=" + m["id"])), cfg["teamWebId"])
                    if pbp and pbp["ev"]:
                        out["pbp"][m["id"]] = pbp
                    if lu:
                        out["lineups"][m["id"]] = lu
                except Exception as e:  # noqa: BLE001
                    log("  match log failed:", e)
        out["series"][key] = {"matches": rows, "table": table}
        info = out["comps"].get(key) or prev.get("comps", {}).get(key)
        if info:
            out["series"][key].update({k: v for k, v in info.items() if k in ("short", "head", "name", "team", "url")})
    return out


def find_rounds(page, cfg, also, prev):
    """A team page lists every competition (Profixio "klasse") the same team is entered in, e.g. the next
    Ungdoms-SM round ("Omg 2") once it is published. Return the ones we don't follow yet, so they get
    their own fixtures and table (scores empty until played)."""
    m = re.search(r"/app/(leagueid\d+)/teams/(\d+)", cfg["url"])
    if not m:
        return {}
    urls = [c["url"] for c in all_series(prev).values()] + [c["url"] for c in also]
    known = {k for u in urls for k in re.findall(r"[?&]k=(\d+)", u)}
    new = {}
    for k, label in re.findall(r'wire:click="chooseKlasse\((\d+)\)"[^>]*>\s*([^<]+?)\s*</button>', page):
        label = clean(label)
        if k in known or "k" + k in new or "Pojkar U16" not in label:
            continue
        omg = re.search(r"Omg\.?\s*(\d+)", label, re.I)
        usm = "USM" in label or "Ungdoms-SM" in label
        short = "USM" if usm else "RM" if "RM" in label or "Riksmästerskap" in label else "Serie"
        group = ""
        try:
            cat = get(f"https://www.profixio.com/app/{m.group(1)}/category/{k}")
            i = cat.find(f"teams/{m.group(2)}")
            g = re.findall(r">\s*(Grupp [^<]{1,20}?)\s*<", cat[:i]) if i > 0 else []
            group = g[-1] if g else ""
        except Exception as e:  # noqa: BLE001
            log("  group lookup failed:", e)
        part = " ".join(x for x in [f"Omg {omg.group(1)}" if omg else "", group] if x)
        head = (("USM U16" if usm else short + " U16") + (" · " + part if part else "")).strip()
        name = ("Ungdoms-SM · Pojkar U16" if usm else label) + (f", omgång {omg.group(1)}" if omg else "") + (f" {group.lower()}" if group else "")
        new["k" + k] = {"url": f"https://www.profixio.com/app/{m.group(1)}/teams/{m.group(2)}?k={k}", "teamWebId": int(m.group(2)),
                        "team": cfg.get("team") or OUR, "short": short,
                        "head": head, "name": name, "found": now().strftime("%Y-%m-%d")}
        log("  new round/competition:", head, new["k" + k]["url"])
    return new


def all_series(prev):
    out = dict(SERIES)
    for k, c in (prev.get("comps") or {}).items():
        if k not in out:
            out[k] = {"url": c["url"], "teamWebId": c["teamWebId"], "team": c.get("team")}
    return out


LAST_DISCOVERY = {}


def discover(prev):
    """Look through the club's registrations for competitions P11 Röd has been entered into that we don't follow yet."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("discovery skipped (no browser installed)")
        return prev.get("comps", {})
    comps = dict(prev.get("comps", {}))
    known = {c["teamWebId"] for c in SERIES.values()} | {c["teamWebId"] for c in comps.values()}
    seen = {}
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(locale="sv-SE", user_agent=UA)
            pg.goto(REG_URL, wait_until="networkidle", timeout=90000)
            pg.wait_for_timeout(2000)
            for n in range(1, 30):
                items = pg.eval_on_selector_all('a[href*="/teams/"]', "els => els.map(a => [a.href, a.innerText])")
                seen.update(dict(items))
                nxt = pg.locator(f"button:text-is('{n + 1}'), a:text-is('{n + 1}')").first
                if not nxt.count() or not nxt.is_visible():
                    break
                nxt.click()
                pg.wait_for_timeout(2500)
            b.close()
    except Exception as e:  # noqa: BLE001
        log("discovery failed:", e)
        return comps
    log(f"discovery: {len(seen)} club teams listed")
    LAST_DISCOVERY.update({"at": now().strftime("%Y-%m-%dT%H:%M"), "teamsListed": len(seen)})
    for href, text in seen.items():
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        if not lines:
            continue
        name, meta = lines[0], " | ".join(lines[1:])
        ours = "Pojkar U16" in meta and (name == OUR or (name == "KFUM Blackebergs IK" and ("Ungdoms-SM" in meta or "Riksmästerskap" in meta)))
        m = re.search(r"/app/(leagueid\d+)/teams/(\d+)", href)
        if not ours or not m or int(m.group(2)) in known:
            continue
        key = "l" + m.group(2)
        last = lines[-1].split(" - ")[-1]
        short = "USM" if "Ungdoms-SM" in meta else "RM" if "Riksmästerskap" in meta else "Serie"
        comps[key] = {"url": f"https://www.profixio.com/app/{m.group(1)}/teams/{m.group(2)}", "teamWebId": int(m.group(2)),
                      "team": name, "short": short, "head": last, "name": meta.split(" | ")[-2] if " | " in meta else meta,
                      "found": now().strftime("%Y-%m-%d")}
        log("  new competition:", short, last, href)
    return comps


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
    """Return ('live', end_time) / ('full', reason) / ('skip', reason).

    GitHub sometimes skips or delays scheduled runs, so nothing here depends on running at an exact
    minute: checks are due when the last update is old enough, and live mode starts up to 3 hours
    before tip-off and simply waits for the game.
    """
    if not prev:
        return "full", "first run"
    games = fixtures(prev)
    soon = [k for k, _, done in games if not done and k - LIVE_LEAD <= t <= k + LIVE_AFTER]
    if soon:
        return "live", max(soon) + LIVE_AFTER
    try:
        last = datetime.fromisoformat(prev["updated"]).replace(tzinfo=TZ)
    except Exception:  # noqa: BLE001
        last = t - timedelta(days=9)
    age = t - last
    night = t.replace(hour=NIGHT_START, minute=0, second=0, microsecond=0)
    recent = [k for k, _, _ in games if k + LIVE_AFTER < t <= k + AFTER_GAME]
    if recent and age >= timedelta(minutes=50):
        return "full", "catch-up after a game"
    if t >= night and last < night:
        return "full", "nightly check" if t.hour < NIGHT_END else "daily check (the night run was missed)"
    if t.weekday() >= 4 and 8 <= t.hour <= 21 and age >= WEEKEND_EVERY:
        return "full", "weekend check"
    return "skip", "nothing scheduled"


# ---------- Instagram (the owner keeps the list in instagram.txt) ----------
INSTA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "instagram.txt")
INSTA_MAX = 3
MONTHS_EN = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                         "August", "September", "October", "November", "December"], 1)}


def insta_ids():
    """Reel/post ids from instagram.txt, newest first (the top of the file)."""
    ids = []
    try:
        for line in open(INSTA_FILE, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.search(r"instagram\.com/(?:[\w.]+/)?(?:reel|reels|p)/([\w-]+)", line)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    except FileNotFoundError:
        pass
    return ids[:INSTA_MAX]


def insta_cover(url):
    """Download a cover picture and shrink it to a 240px square WebP (base64). Empty string if not possible."""
    try:
        from PIL import Image  # installed in the workflow
        import base64, io
        r = S.get(url, timeout=30)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGB")
        w, h = im.size
        side = min(w, h)
        im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)).resize((240, 240))
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=60)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:  # noqa: BLE001
        print("  instagram cover failed:", e)
        return ""


def insta_post(rid):
    """Date, short caption and cover for one reel, read from the public post page (one try, no retries)."""
    date, text, cover = "", "", ""
    try:
        r = S.get(f"https://www.instagram.com/reel/{rid}/", timeout=30)
        html_ = r.text if r.ok else ""
    except Exception as e:  # noqa: BLE001
        print("  instagram page failed:", e)
        html_ = ""
    meta = lambda prop: (re.search(r'<meta[^>]+property="%s"[^>]+content="([^"]*)"' % prop, html_) or [None, ""])[1]
    desc, title, img = html.unescape(meta("og:description")), html.unescape(meta("og:title")), html.unescape(meta("og:image"))
    m = re.search(r" on (\w+) (\d{1,2}), (\d{4})", desc)
    if m and m.group(1) in MONTHS_EN:
        date = f"{m.group(3)}-{MONTHS_EN[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(r'"(.*)"', title, re.S) or re.search(r': "(.*)', desc, re.S)
    if m:
        t = re.sub(r"[#@][\w.]+", "", m.group(1)).replace("\n", " ")
        t = re.split(r"(?<=[.!?])\s", re.sub(r"\s+", " ", t).strip())[0]
        text = t if len(t) <= 110 else t[:107].rsplit(" ", 1)[0] + "…"
    if img:
        cover = insta_cover(img)
    time.sleep(1.5)
    return [rid, date, text, cover]


def scrape_insta(prev):
    """Keep data.json "insta" in step with instagram.txt. Only new or incomplete entries are fetched."""
    old = {x[0]: x for x in prev.get("insta", []) if x}
    out = []
    for rid in insta_ids():
        x = old.get(rid)
        if not x or not x[1] or not x[3]:
            log("instagram: fetching", rid)
            y = insta_post(rid)
            x = [rid] + [y[k] or (x[k] if x else "") for k in (1, 2, 3)]
        out.append(x)
    return out


def live_sources(prev, t=None):
    """During a game, which sources have a game in the live window: Profixio (league/USM) and/or Cup Manager (cups).
    Cup games only exist on Cup Manager, so a cup day doesn't need to ask Profixio every 10 minutes (and the other way round)."""
    t = t or now()
    def due(rows, date_i, time_i, done):
        for r in rows:
            if not r[time_i] or done(r):
                continue
            k = datetime.fromisoformat(f"{r[date_i]}T{r[time_i]}").replace(tzinfo=TZ)
            if k - LIVE_BEFORE - timedelta(seconds=LIVE_STEP) <= t <= k + LIVE_AFTER:
                return True
        return False
    prof = due([r for s in prev.get("series", {}).values() for r in s.get("matches", [])], 0, 1, lambda r: r[5] is not None)
    cup = due(prev.get("cupGames", []), 0, 1, lambda g: len(g) > 7 and g[7] is not None)
    return prof, cup


def build(prev, live_only=False, discover_new=False):
    if discover_new:
        prev = dict(prev, comps=discover(prev))
    prof_due, cup_due = live_sources(prev) if live_only else (True, True)
    if prof_due:
        p = scrape_profixio(prev, live_only)
    else:  # live window for a cup game only: keep the Profixio data as it is
        log("Profixio skipped (no league/USM game right now)")
        p = {"series": prev.get("series", {}), "pbp": {}, "lineups": {}, "logos": {}, "comps": {},
             "live": {k: v for k, v in prev.get("live", {}).items() if k not in {str(g[9]) for g in prev.get("cupGames", []) if len(g) > 9}}}
    if cup_due:
        cups, cup_live = scrape_cups(prev)
    else:  # live window for a Profixio game only: keep the cup data as it is
        log("Cup Manager skipped (no cup game right now)")
        cups, cup_live = prev.get("cupGames", []), {}
    data = {
        "updated": now().strftime("%Y-%m-%dT%H:%M"),
        "series": p["series"],
        "cupGames": cups or prev.get("cupGames", []),
        "pbp": {**prev.get("pbp", {}), **p["pbp"]},
        "lineups": {**prev.get("lineups", {}), **p["lineups"]},
        "logos": {**prev.get("logos", {}), **p["logos"]},
        "live": {**p["live"], **cup_live},
        "comps": {**prev.get("comps", {}), **p["comps"]},
        "discovery": LAST_DISCOVERY or prev.get("discovery", {}),
        "insta": prev.get("insta", []) if live_only else scrape_insta(prev),
        "calfeeds": prev.get("calfeeds", []),
    }
    data["final"] = sorted({r[7] for s in data["series"].values() for r in s.get("matches", []) if len(r) > 7 and r[5] is not None and r[7]})
    return data


def save(data):
    try:
        write_ics(data)  # also fills data["calfeeds"]
    except Exception as e:  # noqa: BLE001  (the calendar must never stop the data update)
        log("calendar feed failed:", e)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log("saved data.json", os.path.getsize(OUT), "bytes;", len(data["live"]), "live game(s)")


# ---------- calendar feed ----------

# Calendar feeds: (file, group or None for everything, calendar name). Group = serie / usm (all rounds) / cup.
ICS_FEEDS = [("matcher.ics", None, "P11 Röd – matcher"), ("matcher-serie.ics", "serie", "P11 Röd – serien"),
             ("matcher-usm.ics", "usm", "P11 Röd – USM"), ("matcher-cup.ics", "cup", "P11 Röd – cuper")]
OUR_EXTRA = set()
APP_URL = "https://keithhearne.github.io/bbpered/"
COMP_NAMES = {"serie": "Serie", "usm": "USM"}


def ics_text(t):
    t = str(t or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
    return t


def ics_fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n ".join(out)


def slug(t):
    t = t.lower().translate(str.maketrans("åäöé", "aaoe"))
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-") or "x"


def write_ics(data):
    """Subscribable calendars with P11 Röd's games: all games, the league, all USM rounds, all cups — plus one per
    extra competition the job finds (a new series, RM, …) and one per cup.
    Rewritten on every update, so subscribed calendars pick up new games, time changes and results by themselves.
    Also stores the list of calendars in data["calfeeds"] for the app (with the date each was first seen, for a "Ny" badge)."""
    OUR_EXTRA.update(c.get("team") for c in (data.get("comps") or {}).values() if c.get("team"))
    comps = data.get("comps") or {}
    games = []  # (date, time, home, away, venue, hs, as, id, comp label, url, group, feed key)
    extra = {}  # feed key -> label
    for key, s in (data.get("series") or {}).items():
        short = s.get("short") or COMP_NAMES.get(key, "Serie")
        comp = s.get("head") or short if key not in COMP_NAMES and short == "USM" else short
        group = "usm" if short == "USM" else "serie" if key == "serie" else "x"
        fkey = None if group in ("usm", "serie") else key
        if fkey:
            extra[fkey] = s.get("head") or short
        for r in s.get("matches", []):
            date, tm, home, away, venue, hs, as_, mid = (r + [None] * 8)[:8]
            games.append((date, tm, home, away, venue, hs, as_, mid, comp, None, group, fkey))
    for r in data.get("cupGames") or []:
        date, tm, home, away, venue, url, comp, hs, as_, mid = (r + [None] * 10)[:10]
        fkey = "cup-" + slug(comp or "Cup")  # one calendar per cup (the app only offers them once there are 2+ cups)
        extra[fkey] = comp or "Cup"
        games.append((date, tm, home, away, venue, hs, as_, mid, comp, url, "cup", fkey))
    try:  # stable stamp = time of the data, so the file only changes when the data does
        stamp = datetime.strptime(data["updated"], "%Y-%m-%dT%H:%M").replace(tzinfo=TZ).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    except (KeyError, ValueError):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    today = now().strftime("%Y-%m-%d")
    # Only games still to be played go into the calendars (owner, 28 Oct 2026): no old games, no results.
    games = [g for g in games if g[5] is None and (g[0] or "") >= today]
    cupnames = sorted({g[8] or "Cup" for g in games if g[10] == "cup"})
    seen = {f.get("key"): f.get("since") for f in data.get("calfeeds") or [] if isinstance(f, dict)}
    first_run = not seen  # calendars that already existed when this list was introduced are not "new"
    upcoming = lambda sel: sum(1 for g in sel if g[5] is None and (g[0] or "") >= today)  # noqa: E731
    feeds = []
    for fname, group, calname in ICS_FEEDS:
        sel = [g for g in games if group is None or g[10] == group]
        if group == "cup" and len(cupnames) == 1:  # just one cup: call it by its name
            calname = f"P11 Röd – {cupnames[0]}"
        write_feed(os.path.join(ROOT, fname), sel, calname, stamp)
        key = group or "alla"
        f = {"key": key, "file": fname, "label": cupnames[0] if key == "cup" and len(cupnames) == 1 else {"alla": "Alla", "serie": "Serien", "usm": "USM", "cup": "Cuper"}[key],
             "games": len(sel), "upcoming": upcoming(sel), "since": seen.get(key) or "2026-10-20"}
        if key == "usm":  # a new USM round counts as news for the USM calendar
            rounds = [c for c in comps.values() if c.get("short") == "USM" and c.get("found")]
            if rounds:
                newest = max(rounds, key=lambda c: c["found"])
                f["since"], f["note"] = newest["found"], (newest.get("head") or "Ny omgång").replace("USM U16 · ", "")
        feeds.append(f)
    for fkey, label in extra.items():
        sel = [g for g in games if g[11] == fkey]
        fname = f"matcher-{fkey}.ics"
        write_feed(os.path.join(ROOT, fname), sel, f"P11 Röd – {label}", stamp)
        found = (comps.get(fkey) or {}).get("found")
        feeds.append({"key": fkey, "file": fname, "label": label, "games": len(sel), "upcoming": upcoming(sel),
                      "since": seen.get(fkey) or found or ("2026-10-20" if first_run else today),
                      "extra": "cup" if fkey.startswith("cup-") else "comp"})
    data["calfeeds"] = feeds

def write_feed(path, games, calname, stamp):
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//bbpered//P11 Röd matcher//SV", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
         f"X-WR-CALNAME:{ics_text(calname)}", "X-WR-CALDESC:Blackeberg P11 Röds matcher. Uppdateras automatiskt.",
         "X-WR-TIMEZONE:Europe/Stockholm", "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H",
         "BEGIN:VTIMEZONE", "TZID:Europe/Stockholm",
         "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST", "DTSTART:19700329T020000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
         "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET", "DTSTART:19701025T030000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
         "END:VTIMEZONE"]
    ours = {OUR, "KFUM Blackebergs IK"} | OUR_EXTRA
    for date, tm, home, away, venue, hs, as_, mid, comp, url, *_ in sorted(games, key=lambda g: (g[0] or "", g[1] or "")):
        if not date or not mid:
            continue
        home_us = home in ours
        opp = away if home_us else home
        title = "🏀 " + (f"P11 Röd – {opp}" if home_us else f"{opp} – P11 Röd")
        desc = [comp + (" · Hemma" if home_us and not url else " · Borta" if not url else "")]
        if hs is not None and as_ is not None:
            us, them = (hs, as_) if home_us else (as_, hs)
            res = "Vinst" if us > them else "Förlust"
            title += f" ({hs}–{as_}, {res})"
            desc.append(f"Resultat: {hs}–{as_} ({res})")
        desc.append(f"Allt om matchen: {APP_URL}")
        if url:
            desc.append(url)
        d = date.replace("-", "")
        L += ["BEGIN:VEVENT", f"UID:p11rod-{mid}@bbpered", f"DTSTAMP:{stamp}"]
        if tm:
            start = datetime.strptime(date + " " + tm, "%Y-%m-%d %H:%M")
            end = start + timedelta(minutes=90)
            L += [f"DTSTART;TZID=Europe/Stockholm:{start:%Y%m%dT%H%M%S}", f"DTEND;TZID=Europe/Stockholm:{end:%Y%m%dT%H%M%S}"]
        else:
            nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y%m%d")
            L += [f"DTSTART;VALUE=DATE:{d}", f"DTEND;VALUE=DATE:{nxt}"]
        L += [f"SUMMARY:{ics_text(title)}", f"DESCRIPTION:{ics_text(chr(10).join(desc))}"]
        if venue:
            L.append(f"LOCATION:{ics_text(venue)}")
        L += [f"URL:{url or APP_URL}", "END:VEVENT"]
    L.append("END:VCALENDAR")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(ics_fold(x) for x in L) + "\r\n")
    log("saved", os.path.basename(path) + ",", len(games), "games")


def write_status(result, mode):
    """status.json (read by the app) and STATUS.md (for people): when the job last ran and what it did."""
    st = {}
    if os.path.exists(STATUS_JSON):
        try:
            with open(STATUS_JSON, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            st = {}
    t = now().strftime("%Y-%m-%dT%H:%M")
    hist = ([[t, mode, result]] + st.get("history", []))[:40]
    data_updated = ""
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            data_updated = json.load(f).get("updated", "")
    st = {"lastCheck": t, "lastResult": result, "dataUpdated": data_updated, "history": hist}
    with open(STATUS_JSON, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    rows = "\n".join(f"| {h[0].replace('T', ' ')} | {h[2]} |" for h in hist)
    with open(STATUS_MD, "w", encoding="utf-8") as f:
        f.write(f"""# Update status

This file is rewritten every time the update job runs (Swedish time).

- **Last run:** {t.replace('T', ' ')} — {result}
- **Data last refreshed:** {data_updated.replace('T', ' ') or '–'}

GitHub decides when scheduled runs actually start, and sometimes skips them; see the README.

## Recent runs (newest first)

| Time | What happened |
|---|---|
{rows}
""")


def push_live(data):
    """Write live.json (just the live scores) to the "live" branch through the GitHub API.
    The app reads it straight from GitHub every 2 minutes; pushing to this branch does not rebuild the website."""
    payload = {"updated": now().strftime("%Y-%m-%dT%H:%M:%S"), "live": data.get("live", {}), "final": data.get("final", [])}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(ROOT, "live.json"), "w", encoding="utf-8") as f:
        f.write(body)
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        log("live.json written locally (no GitHub token)")
        return
    api = f"https://api.github.com/repos/{repo}"
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": UA}
    try:
        r = requests.get(f"{api}/git/ref/heads/{LIVE_BRANCH}", headers=h, timeout=20)
        if r.status_code == 404:  # first time: create the branch from main
            main_sha = requests.get(f"{api}/git/ref/heads/main", headers=h, timeout=20).json()["object"]["sha"]
            requests.post(f"{api}/git/refs", headers=h, json={"ref": f"refs/heads/{LIVE_BRANCH}", "sha": main_sha}, timeout=20).raise_for_status()
        cur = requests.get(f"{api}/contents/live.json", headers=h, params={"ref": LIVE_BRANCH}, timeout=20)
        sha = cur.json().get("sha") if cur.ok else None
        import base64
        put = {"message": "Live score", "branch": LIVE_BRANCH, "content": base64.b64encode(body.encode()).decode(),
               "committer": {"name": "p11-bot", "email": "actions@users.noreply.github.com"}}
        if sha:
            put["sha"] = sha
        requests.put(f"{api}/contents/live.json", headers=h, json=put, timeout=20).raise_for_status()
        log("live.json pushed:", len(payload["live"]), "live game(s)")
    except Exception as e:  # noqa: BLE001  (live.json is a bonus; the normal update carries on)
        log("live.json push failed:", e)


def commit(msg):
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    os.system('git config user.name "p11-bot" && git config user.email "actions@users.noreply.github.com"')
    os.system("git add data.json status.json STATUS.md matcher*.ics")
    if os.system("git diff --cached --quiet") != 0:
        os.system(f'git commit -q -m "{msg}" && (git push -q || (git pull -q --rebase && git push -q))')
        log("pushed")


def needs_browser(force, mode, info, prev):
    return mode == "full" and (force or "comps" not in prev or str(info).startswith(("nightly", "daily")))


def main():
    prev = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            prev = json.load(f)
    force = "--force" in sys.argv or os.environ.get("FORCE") == "1"
    t = now()
    mode, info = ("full", "manual run") if force else decide(prev, t)
    log("mode:", mode, info if not isinstance(info, datetime) else "until " + info.strftime("%H:%M"))
    if "--plan" in sys.argv:  # tells the workflow whether to install the browser for this run
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a") as f:
                f.write(f"browser={'true' if needs_browser(force, mode, info, prev) else 'false'}\n")
        return
    if mode == "skip":
        write_status("Checked — nothing due", "skip")
        commit("Status: checked")
        return
    if mode == "full":
        data = build(prev, discover_new=needs_browser(force, mode, info, prev))
        save(data)
        push_live(data)  # keeps live.json in step (empty when no game is on)
        write_status(f"Full update ({info})", "full")
        commit("Update data")
        return
    # Live mode: wait for tip-off, then check the score every 2 minutes until the game window closes.
    # Each check goes to live.json on the "live" branch (no website rebuild); the full data.json is
    # published every 15 minutes and as soon as a game ends.
    end = info
    last_main = None
    while True:
        t = now()
        games = [(k, done) for k, _, done in fixtures(prev) if not done and k <= end]
        active = [k for k, _ in games if k - LIVE_BEFORE <= t <= k + LIVE_AFTER]
        nxt = min((k for k, _ in games if k - LIVE_BEFORE > t), default=None)
        if not active and nxt:
            wait = (nxt - LIVE_BEFORE - t).total_seconds()
            log(f"waiting {int(wait // 60)} min for tip-off at {nxt.strftime('%H:%M')}")
            write_status(f"Game day — waiting for tip-off at {nxt.strftime('%H:%M')}", "live")
            commit("Status: waiting for game")
            time.sleep(wait)
            continue
        finals_before = set(prev.get("final", [])) | {g[9] for g in prev.get("cupGames", []) if len(g) > 9 and g[7] is not None}
        data = build(prev, live_only=True)
        save(data)
        push_live(data)
        finals_now = set(data.get("final", [])) | {g[9] for g in data.get("cupGames", []) if len(g) > 9 and g[7] is not None}
        if last_main is None or now() - last_main >= MAIN_EVERY or finals_now != finals_before:
            write_status(f"Live update ({len(data['live'])} game(s) in progress)" if data["live"] else "Live mode: waiting for / checking the game", "live")
            commit("Live update")
            last_main = now()
        prev = data
        end = max([end] + [k + LIVE_AFTER for k, _, done in fixtures(prev) if not done and k <= now() + LIVE_BEFORE])
        if now() + timedelta(seconds=LIVE_STEP) > end:
            break
        time.sleep(LIVE_STEP)
    data = build(prev)  # full update (tables too) when the window closes
    save(data)
    push_live(data)
    write_status("Full update after the game", "full")
    commit("Update data after game")


if __name__ == "__main__":
    main()
