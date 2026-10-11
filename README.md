# EarthMC Fall Finder

Finds [EarthMC](https://earthmc.net) towns that are about to fall, using the
official API (`https://api.earthmc.net/v4`).

A town is deleted after 42 days with no resident logging in. This tool looks up
every resident's last login and lists towns where the most recent login plus
42 days lands inside your time window.

## Features

- Terminal output, or a self-contained HTML page (`--html`)
- Page filters: gold in bank, chunks, residents, hours left, text search
- Click column headers to sort; works on phones
- Optional Discord webhook and loop mode
- Optional GitHub Actions workflow that publishes the page to GitHub Pages hourly

## Usage

```bash
pip install -r requirements.txt

python earthmc_fall_finder.py                       # next 24h, prints to terminal
python earthmc_fall_finder.py --hours 48            # wider window
python earthmc_fall_finder.py --html falling.html   # also write a web page
python earthmc_fall_finder.py --loop 60             # re-run every 60 minutes
python earthmc_fall_finder.py --webhook <discord webhook url>
```

A full scan takes a couple of minutes because of the API's 180 requests/min limit.

## Auto-updating page on GitHub Pages (no server needed)

1. Push this repo to GitHub.
2. Go to **Settings > Pages** and set **Source** to **GitHub Actions**.
3. Go to the **Actions** tab, open **Update falling towns page** and press **Run workflow**.
4. Your page will be at `https://<your-username>.github.io/<repo-name>/` and refreshes every hour.

## Termux (Android)

```bash
pkg install python git
pip install -r requirements.txt
python earthmc_fall_finder.py --html falling.html
python -m http.server 8000 &     # then open http://localhost:8000/falling.html
```

## Limitations

- Towns held by Premium mayors never fall but can't be detected through the API,
  so towns already past 42 days are not listed.
- Players who opted out of API visibility hide their data, so towns containing
  them are skipped.
- Exact fall time depends on the server's daily check; treat times as approximate.
- If the API rejects large batches, lower `BATCH` in the script.
