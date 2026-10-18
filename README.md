# P11 Röd – match app

A small phone app to follow **Blackeberg Basket P11 Röd** (KFUM Blackebergs IK): fixtures, results, tables, play-by-play (Matchspel) and player stats.
The text in the app is in Swedish.

**Open the app:** https://keithhearne.github.io/bbpered/

On Android (Chrome): open the link → ⋮ menu → **Add to home screen**. It then opens like a normal app, and it still works offline using the last data it saved.

---

## How it stays up to date

```
Profixio (league + USM)  ─┐
                          ├─► GitHub Action (scraper/scrape.py) ─► data.json ─► the app
Cup Manager (cups)       ─┘        runs on GitHub's servers          in this repo   on your phone
```

1. **The update job.** A GitHub Action (`.github/workflows/update.yml`) wakes up **every hour**. It runs the script `scraper/scrape.py`, which first decides whether there is anything to do right now (see the schedule below).
2. **Fetching.** When it is time, the script reads Blackeberg's pages on **Profixio**:
   - fixtures and results
   - league tables
   - play-by-play and line-ups for each game

   It also reads **Cup Manager** for cup games. Everything is saved into one file, **`data.json`**, in the root of this repository.
3. **Publishing.** The job commits `data.json`. GitHub Pages republishes the site, which takes about 1–2 minutes.
4. **The app.** Every time the app opens, it downloads the latest `data.json` and shows it. If the data has changed, the page refreshes itself and keeps you on the same tab or Matchspel page. The app also keeps a copy on the phone, so it works offline.

The app has a full copy of the data built in, from the day it was last published. If `data.json` can't be reached, it shows that instead.

## The schedule

The job wakes every hour, but it only contacts Profixio when there's a reason to.
Most hours it stops after a few seconds, which keeps the load on Profixio light.

| When | What happens |
|---|---|
| **Every night at 03:00** (Swedish time) | Full check: new or changed fixtures, results, tables — and a check of the club's team list on Profixio for **new competitions** P11 Röd has been entered into (see below). |
| **Fridays–Sundays at 09:00 and 14:00** | Extra check for late changes before a game weekend. |
| **Game day: ~1 hour before tip-off** | The job starts and waits for the game. |
| **Game day: from 10 min before tip-off, for ~1 h 40 min** | **Live mode:** score and Matchspel are updated **every 10 minutes**. |
| **Game day: after the live window** | One full update, including tables. |
| **The 6 hours after a game** | Hourly catch-up, to pick up the final score sheet if it's uploaded late. |
| **Any other hour** | Nothing; the job exits straight away. |

The job works out game days by itself from the same fixture list you see in **Kalender**, for league, USM and cup games alike. Weekday games are covered too. Times follow Swedish time, including the summer/winter time change.

### What you see in the app during a game

- **Översikt** shows a **"Pågår nu"** strip with the live score. Tap it to open Matchspel.
- **Matchspel** fills in basket by basket and shows a **LIVE** badge instead of V/F.
- While a game is on, the open app checks for new data every minute. Normally it checks every 15 minutes, and whenever you switch back to it.
- **Delay:** expect the score to be up to about **10–13 minutes** behind real life. It's fetched every 10 minutes, plus 1–3 minutes for GitHub to publish it.
- **It depends on the scorers.** Live data only exists if they enter it in Profixio during the game. If a hall uses a paper score sheet, the result appears once it has been uploaded.

## Common tasks

### Run an update right now
1. Open this repository on GitHub → **Actions** tab.
2. Click **Update data** in the list on the left.
3. Click **Run workflow** → **Run workflow**.

The new data is in the app about 2–3 minutes later.

### Check that updates are running
**Actions** tab → **Update data**. Each run is listed with a green tick ✓ or a red cross ✗. Most runs take under a minute. A live game-day run can take up to about 2½ hours, and that's normal.
You can also open `data.json` and look at `"updated"` near the top.

### If the hourly schedule ever stops
GitHub pauses scheduled jobs in a public repository after **60 days without any activity**. The nightly data commits should count as activity, so this shouldn't happen. If it ever does, GitHub shows a banner on the **Actions** tab, with a button to turn the workflow back on.

### New competitions are picked up automatically
Every night the job looks through Blackeberg's list of teams on Profixio (the club's "registrations" page), going through all of its pages.
It looks for Pojkar U16 entries that belong to P11 Röd: **"KFUM Blackebergs IK Röd"** in a Stockholm series, or **"KFUM Blackebergs IK"** in Ungdoms-SM or Riksmästerskap.

When it finds one that isn't followed yet, it starts following it straight away. A new USM round, for example, then shows up in the app the next morning:
- its games appear in **Kalender**, **Översikt** and **Resultat**
- its table appears in **Tabeller** and **Våra turneringar**
- its label is in teal (for example "USM 2"), and it gets its own filter button

New games, times and results *inside* competitions we already follow (league, USM, and the cup's knock-out games on Cup Manager) were already picked up every night.

You can see when the check last ran, and how many club teams it looked through, under `"discovery"` in `data.json`.

Two limits:
- **New season:** the club list is per season. When the 27/28 season starts, the address in `REG_URL` (in `scraper/scrape.py`) needs updating once.
- **Cup Manager cups:** cups run on Cup Manager are not listed on Profixio, so each new one is still added by hand (below). Their knock-out games do appear automatically once the groups are finished.

### Add a new cup
Cups are not on Profixio, so each new cup is added by hand in `scraper/scrape.py` under `CUPS`. That needs the cup's Cup Manager address, plus its `tournamentId` and `categoryId`. The easiest way is to ask Claude to add it, giving the cup's link.

### Add a new Profixio series
For example a new USM round: it is added under `SERIES` in `scraper/scrape.py`, and also in the app's `SERIES`. Ask Claude, giving the team's Profixio link.

## Cost

**Free.** GitHub Actions is free for public repositories using GitHub's standard runners, and GitHub Pages is free for public repositories.
If the repository is ever made private, the job would only have a limited number of free minutes per month.

## Files

| File | What it is |
|---|---|
| `index.html` | The app: one page with the design, the code and a built-in copy of the data. |
| `data.json` | The latest data, written by the update job. Don't edit it by hand. |
| `scraper/scrape.py` | The update script: fetching, the schedule and live mode. Settings are at the top: `LIVE_STEP` (10 min), `NIGHTLY_HOUR` (03), `WEEKEND_CHECK_HOURS` (09, 14), `LIVE_BEFORE`/`LIVE_AFTER`. |
| `.github/workflows/update.yml` | Tells GitHub to run the script every hour, and when you click "Run workflow". |
| `sw.js`, `manifest.webmanifest`, icons | Make the site installable on the home screen and usable offline. |

## Good to know

- **Player photos** are only in the owner's private copy of the app, not on this public site.
- **The private copy** (a Claude artifact) doesn't update itself. Use the public app for live data.
- **Backup:** the version from before the October 2026 redesign is kept on the branch `backup-before-v2`.
- **Polite fetching:** the script identifies itself, fetches only this team's pages, and waits between requests.
