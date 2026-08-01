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

## State flag images

Each state entry in `rankings.json` now includes a `flag_url` field, e.g.:

```json
{
  "rank": 1,
  "name": "Ohio Regiment",
  "amount": 69042.42,
  "url": "https://car4ukraine.com/campaigns/50forua-ohio-battalion",
  "flag_url": "https://rentry.github.io/50-states-ranking/assets/flags/ohio.svg"
}
```

These are public-domain flag SVGs (50 states + DC + American Samoa, Guam,
Puerto Rico, Northern Mariana Islands, US Virgin Islands - 56 total),
downloaded once from
[nibsbin/us-state-flags-svg](https://github.com/nibsbin/us-state-flags-svg)
(itself sourced from Wikipedia public domain images) and committed directly
into this repo under `assets/flags/`. They're served by the same GitHub
Pages site as the JSON data, so there's no per-run network cost - the
hourly scraper just does a local dictionary lookup in `state_meta.json` to
attach the right `flag_url` to each state.

### How the name matching works

car4ukraine.com's display names are inconsistent ("Ohio Regiment",
"Delaware Battalion", plain "New Mexico" with no suffix at all), so the
scraper strips a trailing " Battalion"/" Regiment" and normalizes
whitespace/punctuation before looking the result up in `state_meta.json`.
That file maps normalized names (and a few aliases, like "dc" -> District
of Columbia) to a flag file.

If a state's display name doesn't match anything, `flag_url` is simply
`null` for that state (nothing else about that state's data is affected),
and a line like this appears in the scraper's output / GitHub Action logs:

```
WARNING: no flag match for state name 'Some New State'
```

That's your signal to add an alias. All 50 states, DC, and the 5 US
territories are already covered, so this should only come up if a state's
display text is unusually phrased.

### Adding a flag alias or a genuinely new flag

Most of the time you won't need new artwork - just a new alias. Open
`scripts/build_state_meta.py`, find the relevant entry in the `STATES`
dict, and add the new phrase to its alias list, e.g.:

```python
"West Virginia": ("Flag_of_West_Virginia.svg", ["wv", "the new phrase"]),
```

Then re-run it (this regenerates `state_meta.json` and re-copies flags -
it needs the source SVGs cloned locally first):

```bash
git clone https://github.com/nibsbin/us-state-flags-svg scripts/us-state-flags-svg
python3 scripts/build_state_meta.py
```

Commit the updated `state_meta.json` (and any new file under
`assets/flags/`, if you ever add a jurisdiction outside the 56 already
covered). This is a manual, occasional step - it is **not** run by the
hourly GitHub Action.

## If the scraper breaks

The page structure at car4ukraine.com could change at any time — this
isn't an official API, so there's no changelog or notice. If a run
produces `ERROR: No state rankings were found on the page...`, that's
the signal. Open the live page, view source, and check whether the
state-ranking links still look like
`<a href="/campaigns/50forua-{state}-battalion">1 State Name $1,234.56</a>`.
If the format changed, `parse_states()` in `scrape_rankings.py` needs
updating — the regex is `STATE_LINE_RE` near the top of the file.

## Setting this up as a public GitHub repo

From inside your `50-states-ranking` folder:

```bash
git init
git add .
git commit -m "Initial commit: 50 States for Ukraine rankings scraper"
```

Then on github.com:

1. Create a new **public** repo (e.g. `50-states-ranking`) - public is required so
   the Wix site can fetch the JSON files with no authentication.
2. Do **not** initialize it with a README/gitignore/license on GitHub's side
   (you already have those locally) - just create the bare repo.
3. Back in your terminal, connect and push:

```bash
git remote add origin https://github.com/<your-username>/50-states-ranking.git
git branch -M main
git push -u origin main
```

### Turning on the scheduled Action

The workflow file at `.github/workflows/scrape.yml` runs automatically once
it's pushed - no extra setup needed. You can watch it run (or trigger it
manually) from the repo's **Actions** tab on GitHub. The first scheduled run
will happen at the next top-of-the-hour after your push; use the "Run workflow"
button under Actions if you don't want to wait.

Because the workflow has `permissions: contents: write`, it's able to commit
`data/*.json` back into the repo automatically after each scrape - you don't
need to do anything for that to keep happening.

### Serving the data to Wix (GitHub Pages)

Once the repo is public and the Action has run at least once:

1. Repo **Settings** â†’ **Pages**
2. Under "Build and deployment," set Source to **Deploy from a branch**
3. Branch: `main`, folder: `/ (root)`
4. Save. GitHub will give you a URL like
   `https://<your-username>.github.io/50-states-ranking/`

The file Wix will fetch is then:

```
https://<your-username>.github.io/50-states-ranking/data/rankings.json
```

GitHub Pages updates can take a minute or two to reflect the latest commit,
which is fine given we're only updating hourly anyway.

## Rate/politeness notes

- This hits the page once per scheduled run (see the GitHub Action —
  hourly by default), not on every site visitor's page load. That's a
  very light load on car4ukraine.com's server.
- The script sends a descriptive User-Agent identifying itself and
  linking back to your site, since it's ultimately supporting the same
  cause they're raising money for.
