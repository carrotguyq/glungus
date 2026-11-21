# EarthMC Fall Finder

Finds [EarthMC](https://earthmc.net) towns and nation capitals that are about to change
mayor or fall, using the official API (`https://api.earthmc.net/v4`). It builds a single
web page you can open on a phone or PC, or host for free on GitHub Pages.

## How it decides

- A mayor who has not logged in for **42 days** loses the town at the next **new day**
  ( New Day is at 10:00 UTC).
- The town goes to the **active councillor who has been in the town the longest**.
  Inactive councillors are skipped.
- With no active councillor it goes to a regular resident. You choose the rule on the page:
  **longest in town** (earliest join date, the default) or **most active** (most recent login).
- With nobody active left, the town **falls**.
- The **Nations** tab lists nation capitals only. The king is the capital's mayor, so when
  the capital changes hands the king changes, or the capital falls.

Playtime is not available from the API, so "longest in town" is used as the closest measure.

## The page

- **Towns** and **Nations** tabs.
- **Happening within:** 6 hours up to 2 weeks.
- **Filters:** search, outcome (falls or mayor changes), open or closed, who it goes to
  (councillor or resident), gold in bank, chunks, residents, towns in nation, watchlist only.
- **Heir is:** switch between longest in town and most active. It changes the outcome text,
  the nation's new king and the highlighted player in the popup.
- **Sorting:** a "Sort by" menu, or tap column headers. Turn on **Multi-sort** to tap several
  headers and sort by more than one column (priority numbers show next to the arrows).
- **Town popup:** tap a row to see every player's role, last login, time in the town and time
  until they count as inactive, plus the winner and how far ahead of the next candidate they are.
- **Watchlist:** tap the star to watch a town. Watched towns stay on top and are remembered
  in your browser. In the popup you can also copy the `/t join` command for open towns.

## API key (required)

Since 18 Nov 2026 the EarthMC API needs a key on every endpoint.

1. In game, run `/api key create`. Never share the key or put it in the code.
2. Provide it as the `EMC_API_KEY` environment variable.

## Usage

```bash
pip install -r requirements.txt
export EMC_API_KEY=your-key-here        # on Windows: set EMC_API_KEY=your-key-here

python earthmc_fall_finder.py                         # next 24h, prints to the terminal
python earthmc_fall_finder.py --hours 48              # wider terminal window
python earthmc_fall_finder.py --html falling.html     # also write the web page (next 14 days)
python earthmc_fall_finder.py --html falling.html --html-hours 168   # page looks 1 week ahead
python earthmc_fall_finder.py --loop 60               # re-run every 60 minutes
python earthmc_fall_finder.py --webhook <discord webhook url>
python earthmc_fall_finder.py --newday-hour 10        # UTC hour of the new day
```

A full scan takes a few minutes because the API allows 180 requests per minute.

## Auto-updating page on GitHub Pages (no server needed)

1. Push this repo to GitHub.
2. **Settings > Secrets and variables > Actions > New repository secret.** Name it
   `EMC_API_KEY` and paste your key. The page and logs never show it.
3. **Settings > Pages**, set **Source** to **GitHub Actions**.
4. In `.github/workflows/update.yml` the step that runs the script must pass the secret:
   ```yaml
   - run: mkdir site && python earthmc_fall_finder.py --html site/index.html
     env:
       EMC_API_KEY: ${{ secrets.EMC_API_KEY }}
   ```
5. Open the **Actions** tab, pick **Update falling towns page** and press **Run workflow**.
6. Your page will be at `https://<your-username>.github.io/<repo-name>/` and refreshes every hour.

Visitors only load the finished page. They never talk to the API, so they do not need a key.

## Termux (Android)

```bash
pkg install python git
pip install -r requirements.txt
export EMC_API_KEY=your-key-here
python earthmc_fall_finder.py --html falling.html
python -m http.server 8000 &     # then open http://localhost:8000/falling.html
```

## Limitations

- Premium mayors keep their town, but Premium status is not in the API. A mayor who is already
  past 42 days and still mayor is skipped as probably Premium.
- Players who opted out of the API hide their data, so towns containing them are skipped.
- Times are approximate. They depend on the server's daily check.
- Mayors and successors are worked out from last login and join date only. The real in-game
  rules may differ in edge cases.
- If the API rejects large batches, lower `BATCH` in the script.
