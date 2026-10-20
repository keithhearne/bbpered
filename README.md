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

1. **The update job.** A GitHub Action (`.github/workflows/update.yml`) wakes up **twice an hour**. It runs the script `scraper/scrape.py`, which first decides whether there is anything to do right now (see the schedule below).
2. **Fetching.** When it is time, the script reads Blackeberg's pages on **Profixio**:
   - fixtures and results
   - league tables
   - play-by-play and line-ups for each game

   It also reads **Cup Manager** for cup games. Everything is saved into one file, **`data.json`**, in the root of this repository.
3. **Publishing.** The job commits `data.json`. GitHub Pages republishes the site, which takes about 1–2 minutes.
4. **The app.** Every time the app opens, it downloads the latest `data.json` and shows it. If the data has changed, the page refreshes itself and keeps you on the same tab or Matchspel page. The app also keeps a copy on the phone, so it works offline.

The app has a full copy of the data built in, from the day it was last published. If `data.json` can't be reached, it shows that instead.

## The schedule

The job wakes **twice an hour**, but it only contacts Profixio when there's a reason to.
Most runs stop after a few seconds, which keeps the load on Profixio light.

| When | What happens |
|---|---|
| **Every night: the first run after 03:00** (Swedish time) | Full check: new or changed fixtures, results and tables. It also checks the club's team list on Profixio for **new competitions** P11 Röd has been entered into (see below). |
| **Fridays–Sundays, 08:00–22:00** | A check at least every 5 hours, for late changes before a game weekend. |
| **Game day: from ~3 hours before tip-off** | The first run in this window stays on and waits for the game. |
| **Game day: from 10 min before tip-off, for ~1 h 40 min** | **Live mode:** score and Matchspel are updated **every 10 minutes**. |
| **Game day: after the live window** | One full update, including tables. |
| **The 6 hours after a game** | Hourly catch-up, to pick up the final score sheet if it's uploaded late. |
| **Any other time** | Nothing; the job exits straight away. |

GitHub doesn't guarantee that scheduled runs happen on time, and it sometimes skips them altogether. On the first night (18–19 Oct 2026) it skipped the 03:00 run. So the job doesn't rely on exact times:
- **Night check:** if no full update has happened since 03:00, the next run that does happen does one, even if that's in the morning.
- **Live mode:** it starts up to 3 hours before tip-off, so one missed run doesn't matter.

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
- **In the app:** at the bottom of **Översikt** it says, for example, *"Data uppdaterad 19 okt 22:13 · kontrollerad 19 okt 22:31"*. "Data uppdaterad" is when new data was last fetched. "Kontrollerad" is the last time the job ran, even if it found nothing to do.
- **`STATUS.md`** in this repository lists the last run and the 40 most recent runs, with what each one did: "Checked — nothing due", "Full update (nightly check)", "Live update", and so on. `status.json` holds the same information for the app.
- **Actions tab → Update data:** every run with a green tick ✓ or red cross ✗. Most runs take under a minute; a game-day run can take up to about 4½ hours, because it waits for tip-off.

### If the hourly schedule ever stops
GitHub pauses scheduled jobs in a public repository after **60 days without any activity**. The nightly data commits should count as activity, so this shouldn't happen. If it ever does, GitHub shows a banner on the **Actions** tab, with a button to turn the workflow back on.

### New competitions are picked up automatically
Every night the job looks through Blackeberg's list of teams on Profixio (the club's "registrations" page). It uses the page's own search filter, "Pojkar U16", so it only reads the club's ~5 Pojkar U16 teams instead of all ~73.
It looks for Pojkar U16 entries that belong to P11 Röd: **"KFUM Blackebergs IK Röd"** in a Stockholm series, or **"KFUM Blackebergs IK"** in Ungdoms-SM or Riksmästerskap.

**New rounds of a competition we already follow** (for example USM "Omg 2" after "Omg 1") are found a different way: Profixio lists every round the team is entered in on the team's own page. Every full update (not just the nightly one) looks at that list, so a newly published round is picked up at the next full update. That is how USM Omg 2 (6–8 November) was found.

When it finds one that isn't followed yet, it starts following it straight away. A new USM round, for example, then shows up in the app:
- its games appear in **Kalender**, **Översikt** and **Resultat**
- its table appears in **Tabeller** and **Våra turneringar**: before any games are played, the table shows dashes and Våra turneringar shows the start date
- it gets its own label (for example "USM 2"; USM rounds keep the amber USM colour, other new competitions are teal) and, once a game has been played, its own filter button

New games, times and results *inside* competitions we already follow (league, USM, and the cup's knock-out games on Cup Manager) were already picked up every night.

You can see when the check last ran, and how many club teams it looked through, under `"discovery"` in `data.json`.

Two limits:
- **New season:** the club list is per season. When the 27/28 season starts, the address in `REG_URL` (in `scraper/scrape.py`) needs updating once.
- **Cup Manager cups:** cups run on Cup Manager are not listed on Profixio, so each new one is still added by hand (below). Their knock-out games do appear automatically once the groups are finished.

### The calendar feeds (matcher*.ics)
Every update also writes four calendars with P11 Röd's games: **`matcher.ics`** (all games), **`matcher-serie.ics`** (the league), **`matcher-usm.ics`** (all USM rounds) and **`matcher-cup.ics`** (all cups). People subscribe once, and new games, changed times and results then show up in their calendar by themselves (Google Calendar checks a few times a day, so changes can take some hours). They are plain text lists of games; nothing is installed on anyone's phone.
The collapsible "Lägg till matcherna i din kalender" box at the top of **Kalender** lets people pick which games and subscribe. It is only shown in the private version for now; set `PUBLIC_CAL_FEED = True` in `build_site.py` (or ask Claude) to show it on the public site.

### Add a new Instagram video
The **@Instagram** section on Översikt shows the 3 newest videos listed in **`instagram.txt`** in this repository.
1. In Instagram, open the video → **Share** → **Copy link**.
2. On GitHub, open `instagram.txt` → the pencil icon (**Edit**).
3. Paste the link on a new line at the **top** of the list, then click **Commit changes**.

The next update run (at the latest the nightly check, or straight away with **Run workflow**) reads the date and caption from Instagram and fetches the cover picture. If Instagram refuses to give the job the cover picture, the video shows as a red tile with a play button instead; ask Claude to fill in the cover. The private copy shows new videos only when Claude republishes it.

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
| `instagram.txt` | The Instagram video links shown on Översikt (newest at the top). You can edit this one. |
| `data.json` | The latest data, written by the update job. Don't edit it by hand. |
| `STATUS.md`, `status.json` | When the job last ran and what it did (rewritten on every run). |
| `scraper/scrape.py` | The update script: fetching, the schedule and live mode. Settings are at the top: `LIVE_STEP` (10 min), `NIGHT_START` (03), `WEEKEND_EVERY` (5 h), `LIVE_LEAD` (3 h), `LIVE_BEFORE`/`LIVE_AFTER`. |
| `.github/workflows/update.yml` | Tells GitHub to run the script twice an hour, and when you click "Run workflow". |
| `sw.js`, `manifest.webmanifest`, icons | Make the site installable on the home screen and usable offline. |

## Good to know

- **Player photos** are shown in Trupp and in the Matchspel scorer list (the parents agreed, 19 Oct 2026). They can be removed from the public site again with one switch (`PUBLIC_PHOTOS` in the build script).
- **The private copy** (a Claude artifact) doesn't update itself. Use the public app for live data.
- **Backup:** the version from before the October 2026 redesign is kept on the branch `backup-before-v2`.
- **Polite fetching:** the script identifies itself, fetches only this team's pages, and waits between requests.
