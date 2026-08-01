# 50 States for Ukraine — Rankings Scraper

Scrapes the live campaign page at https://car4ukraine.com/campaigns/50forua
and produces JSON data for the 50statesforukraine.com Wix site.

## Local test

```bash
pip install -r requirements.txt
python3 scrape_rankings.py
```

On success you'll see something like:

```
OK: 32 states, total donated = 542338.64
```

And three files will appear under `data/`:

- **`data/history.jsonl`** — append-only log, one JSON snapshot per line.
  This is what future runs use to compute "biggest gain this week."
  Commit this file so history accumulates across GitHub Action runs.
- **`data/latest.json`** — just the most recent snapshot, no deltas.
- **`data/rankings.json`** — the file the Wix site actually reads. Latest
  snapshot plus `delta_7d` per state and a `top_movers_7d` list, already
  sorted by biggest 7-day gain.

## If the scraper breaks

The page structure at car4ukraine.com could change at any time — this
isn't an official API, so there's no changelog or notice. If a run
produces `ERROR: No state rankings were found on the page...`, that's
the signal. Open the live page, view source, and check whether the
state-ranking links still look like
`<a href="/campaigns/50forua-{state}-battalion">1 State Name $1,234.56</a>`.
If the format changed, `parse_states()` in `scrape_rankings.py` needs
updating — the regex is `STATE_LINE_RE` near the top of the file.

## Rate/politeness notes

- This hits the page once per scheduled run (see the GitHub Action —
  hourly by default), not on every site visitor's page load. That's a
  very light load on car4ukraine.com's server.
- The script sends a descriptive User-Agent identifying itself and
  linking back to your site, since it's ultimately supporting the same
  cause they're raising money for.
