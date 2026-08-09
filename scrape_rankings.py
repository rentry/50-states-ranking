#!/usr/bin/env python3
"""
Scrapes the 50 States for Ukraine campaign page on car4ukraine.com and
produces a timestamped snapshot of state-level fundraising rankings.

Usage:
    python3 scrape_rankings.py

Output:
    Writes/updates data/history.jsonl   (one JSON snapshot per line, append-only)
    Writes data/latest.json             (most recent snapshot, for easy consumption)
    Writes data/rankings.json           (latest snapshot + 7-day deltas, for the website)
"""

import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

CAMPAIGN_URL = "https://car4ukraine.com/campaigns/50forua"
USER_AGENT = (
    "50StatesForUkraine-RankingsBot/1.0 "
    "(+https://www.50statesforukraine.com; contact via GitHub repo issues)"
)

DATA_DIR = Path(__file__).resolve().parent / "data"
HISTORY_FILE = DATA_DIR / "history.jsonl"
LATEST_FILE = DATA_DIR / "latest.json"
RANKINGS_FILE = DATA_DIR / "rankings.json"

STATE_META_FILE = Path(__file__).resolve().parent / "state_meta.json"

# Published Google Sheet CSV URL for the vehicle ledger (one row per truck/
# vehicle, maintained by Colin). Publish via File > Share > Publish to web >
# CSV in Google Sheets, then paste the URL here. Leave blank to skip vehicle
# data entirely (nothing else breaks - rank/amount still work from the
# car4ukraine scrape alone).
VEHICLE_LEDGER_URL = "https://docs.google.com/spreadsheets/d/1UNjVYb94PSoxa25jGjXECWSQGzSnNQwMRQE2PVw-T5Y/export?format=csv&gid=1696411874"

# Published Google Sheet CSV URL for the merch/shop links tab (state ->
# Fourthwall collection URL, one row per state, maintained by Colin).
# Publish that specific tab via File > Share > Publish to web > CSV, then
# paste its URL here. Leave blank to skip merch links entirely (nothing
# else breaks).
MERCH_SHEET_URL = "https://docs.google.com/spreadsheets/d/1UNjVYb94PSoxa25jGjXECWSQGzSnNQwMRQE2PVw-T5Y/export?format=csv&gid=1595623771"

COUNTED_STATUSES = {"9 delivered", "8 deployed"}  # normalized (lowercased/stripped) match targets
HELP99_PARTNER = "help99"

# Bumped whenever a change alters the *meaning* of a field already stored
# in history.jsonl (e.g. combining Help99 dollars into `amount`, which
# happened at version 2). Snapshots older than the current version are
# not valid delta baselines - comparing across a meaning-change produces
# a misleading number, not a real week-over-week figure. Bump this again
# any time a similar change happens, and old history will automatically
# stop being used as a baseline rather than silently corrupting deltas.
SCHEMA_VERSION = 2

# Separate from SCHEMA_VERSION on purpose - this tracks changes to what a
# rank NUMBER means (e.g. today's switch from "ties share a rank" to
# "dollar amount breaks every tie into a unique rank"), independent of
# whether dollar/vehicle amounts themselves are comparable. Bumping this
# doesn't affect dollar/vehicle delta calculations at all - it only gates
# whether a historical rank is safe to compare against for rank-movement
# tracking specifically.
RANK_VERSION = 2

# Names that legitimately appear in "State Battalion" but aren't states/
# territories (e.g. city-level campaigns). Silently ignored, no warning -
# this is expected, not a data-quality issue. Add to this set as needed.
KNOWN_NON_STATE_NAMES = {"chicago"}


def gh_warning(message: str) -> None:
    """Prints a GitHub Actions warning annotation (visible as a banner on
    the run summary, not just buried in log text) in addition to a plain
    stderr line for local runs."""
    print(f"::warning::{message}")
    print(f"WARNING: {message}", file=sys.stderr)


def fetch_vehicle_ledger(url: str) -> list[dict]:
    """Fetches and parses the vehicle ledger CSV. Non-fatal on failure -
    returns an empty list so the core scrape still succeeds without it."""
    if not url:
        return []
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch vehicle ledger ({exc}); skipping", file=sys.stderr)
        return []
    return list(csv.DictReader(io.StringIO(resp.text)))


def parse_state_weights(weights_field: str) -> list[tuple[str, float]]:
    """Parses 'California:0.5, Texas:0.5' into [('California', 0.5), ('Texas', 0.5)]."""
    pairs = []
    for chunk in weights_field.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        name, _, weight_str = chunk.partition(":")
        try:
            pairs.append((name.strip(), float(weight_str.strip())))
        except ValueError:
            gh_warning(f"bad weight value in State Weights: {chunk!r}")
    return pairs


def parse_money(value: str):
    """Parses a currency string like '$26,953.34' into a float. Returns
    None for empty/unparseable input (logged as a warning if non-empty)."""
    if not value or not value.strip():
        return None
    cleaned = value.replace("$", "").replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        gh_warning(f"could not parse money value from ledger: {value!r}")
        return None


def aggregate_vehicle_data(rows: list[dict], known_state_keys: set) -> tuple[dict, dict]:
    """
    Aggregates the vehicle ledger into:
      - vehicles_by_state: per-state vehicle counts (float, supports
        fractional/weighted credit). Only rows with a status in
        COUNTED_STATUSES are counted here.
      - help99_dollars_by_state: per-state Help99 dollar totals, from the
        "Fundraised Amount" column. NOT filtered by status - funds raised
        toward a vehicle still count even before it's fully delivered.
        A state's presence here (nonzero) is also what determines whether
        its displayed dollar total includes Help99 money and therefore
        gets the explanatory asterisk - not vehicle-delivery status.

    Multi-state rows use "State Weights" if present, otherwise split
    equally across the listed states, for both vehicles and dollars.
    Names that don't match a known state (e.g. "Chicago") are skipped
    with a note, not an error - checked once per row regardless of
    status, so a typo in a not-yet-delivered row is still caught.
    """
    vehicles_by_state = {}
    help99_dollars_by_state = {}

    for row in rows:
        status = (row.get("Status") or "").strip().lower()
        battalion_field = (row.get("State Battalion") or "").strip()
        weights_field = (row.get("State Weights") or "").strip()
        is_help99 = (row.get("Partner") or "").strip().lower() == HELP99_PARTNER

        if weights_field:
            pairs = parse_state_weights(weights_field)
        else:
            names = [n.strip() for n in battalion_field.split(",") if n.strip()]
            weight = 1.0 / len(names) if names else 0
            pairs = [(n, weight) for n in names]

        resolved_pairs = []
        for name, weight in pairs:
            key = normalize_name(name)
            if key not in known_state_keys:
                if key in KNOWN_NON_STATE_NAMES:
                    print(f"INFO: ignoring known non-state entry '{name}' (row: {row.get('Name', '?')})", file=sys.stderr)
                else:
                    gh_warning(
                        f"unrecognized name '{name}' in row '{row.get('Name', '?')}' - "
                        f"possible typo in the sheet? This state's credit was NOT counted."
                    )
                continue
            resolved_pairs.append((key, weight))

        if status in COUNTED_STATUSES:
            for key, weight in resolved_pairs:
                vehicles_by_state[key] = vehicles_by_state.get(key, 0.0) + weight

        if is_help99:
            amount = parse_money(row.get("Fundraised Amount"))
            if amount is not None:
                for key, weight in resolved_pairs:
                    help99_dollars_by_state[key] = help99_dollars_by_state.get(key, 0.0) + amount * weight

    return vehicles_by_state, help99_dollars_by_state


def attach_vehicle_data(states: list[dict], vehicles_by_state: dict, help99_dollars_by_state: dict) -> list[dict]:
    enriched = []
    for s in states:
        stripped = NAME_SUFFIX_RE.sub("", s["name"]).strip()
        key = normalize_name(stripped)
        enriched.append({
            **s,
            "vehicles_delivered": round(vehicles_by_state.get(key, 0.0), 4),
            "includes_help99": bool(help99_dollars_by_state.get(key)),
        })
    return enriched


def add_missing_help99_only_states(
    states: list[dict], vehicles_by_state: dict, help99_dollars_by_state: dict, state_meta: dict
) -> list[dict]:
    """
    Some states may have only ever fundraised through Help99/NAFO and
    never appeared on car4ukraine.com at all - so they wouldn't be in
    `states` yet. This adds a synthetic entry for any such state found in
    the vehicle ledger (by vehicle count or dollar total) that isn't
    already present. Fully automatic - no hardcoded state names - so any
    future Help99-only state is picked up the same way, not just the
    known current examples (New York, Massachusetts).

    Synthetic entries have url=None (nothing on car4ukraine to link to),
    so the widget's existing "no url -> no donate button, row not
    clickable" logic already handles them correctly with no extra work.
    fundraising_rank is None since car4ukraine never assigned them one.
    """
    existing_keys = set()
    for s in states:
        stripped = NAME_SUFFIX_RE.sub("", s["name"]).strip()
        existing_keys.add(normalize_name(stripped))

    candidate_keys = set(vehicles_by_state.keys()) | set(help99_dollars_by_state.keys())
    missing_keys = candidate_keys - existing_keys

    result = list(states)
    for key in sorted(missing_keys):
        meta_entry = state_meta.get(key)
        canonical_name = meta_entry["canonical_name"] if meta_entry else key.title()
        result.append({
            "rank": None,
            "name": canonical_name,
            "amount": 0.0,
            "url": None,
            "fundraising_rank": None,
        })
        print(f"INFO: added Help99-only state not on car4ukraine: '{canonical_name}'", file=sys.stderr)
    return result


def combine_help99_dollars(states: list[dict], help99_dollars_by_state: dict) -> list[dict]:
    """
    Adds each state's Help99 dollar total on top of its car4ukraine
    amount (which must already be car4ukraine-only at this point - never
    read from the ledger - to avoid double-counting). Returns the total
    Help99 dollars added across all states too, so the caller can adjust
    the overall campaign total for consistency.
    """
    enriched = []
    total_help99_dollars = 0.0
    for s in states:
        stripped = NAME_SUFFIX_RE.sub("", s["name"]).strip()
        key = normalize_name(stripped)
        help99_dollars = help99_dollars_by_state.get(key, 0.0)
        total_help99_dollars += help99_dollars
        enriched.append({**s, "amount": round(s["amount"] + help99_dollars, 2)})
    return enriched, round(total_help99_dollars, 2)


def rerank_by_vehicles(states: list[dict]) -> list[dict]:
    """
    Re-sorts and re-ranks states with vehicles_delivered as the primary
    key (descending) and amount as the secondary key (descending). Amount
    now breaks ties into distinct rank numbers, not just list order -
    two states only share a rank on a true dead-heat (identical vehicles
    AND identical amount, which should be extremely rare in practice).
    The original car4ukraine rank is preserved separately as
    fundraising_rank for reference.
    """
    ordered = sorted(states, key=lambda s: (-s.get("vehicles_delivered", 0.0), -s["amount"]))

    reranked = []
    current_rank = 0
    prev_key = None
    for i, s in enumerate(ordered):
        key = (s.get("vehicles_delivered", 0.0), s["amount"])
        if key != prev_key:
            current_rank = i + 1
            prev_key = key
        reranked.append({
            **s,
            "fundraising_rank": s["rank"],
            "rank": current_rank,
        })
    return reranked

# Set this once you know your GitHub Pages URL, e.g.
# "https://your-username.github.io/50-states-ranking"
# If left blank, flag_url in the output will be a relative path instead
# of a full URL (e.g. "assets/flags/ohio.svg").
PAGES_BASE_URL = "https://brentryanjohnson.com/50-states-ranking"

STATE_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s+\$([\d,]+\.\d{2})\s*$")
CAMPAIGN_LINK_RE = re.compile(r"/campaigns/50forua-")
NAME_SUFFIX_RE = re.compile(r"\s+(battalion|regiment)$", re.IGNORECASE)

# Overall campaign totals, e.g. "Donated$542,338.64"
DONATED_RE = re.compile(r"Donated\s*\$([\d,]+\.\d{2})")
DONATIONS_COUNT_RE = re.compile(r"Donations\s*([\d,]+)")
GOAL_RE = re.compile(r"Goal\s*\$([\d,]+(?:\.\d{2})?)")


def normalize_name(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace - used to match a
    scraped display name (e.g. "DC Battalion") against state_meta.json."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_state_meta() -> dict:
    if not STATE_META_FILE.exists():
        return {}
    with open(STATE_META_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def attach_flags(states: list[dict], state_meta: dict) -> list[dict]:
    enriched = []
    for s in states:
        stripped = NAME_SUFFIX_RE.sub("", s["name"]).strip()
        key = normalize_name(stripped)
        entry = state_meta.get(key)

        flag_url = None
        if entry:
            flag_url = (
                f"{PAGES_BASE_URL.rstrip('/')}/{entry['flag_file']}"
                if PAGES_BASE_URL
                else entry["flag_file"]
            )
        else:
            print(f"WARNING: no flag match for state name '{s['name']}'", file=sys.stderr)

        enriched.append({**s, "flag_url": flag_url})
    return enriched


def fetch_merch_links(url: str) -> dict:
    """Fetches and parses the merch/shop-link sheet. Returns a dict keyed
    by normalized state name -> shop URL. Non-fatal on failure - returns
    an empty dict so the core scrape still succeeds without it."""
    if not url:
        return {}
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: could not fetch merch links ({exc}); skipping", file=sys.stderr)
        return {}

    lookup = {}
    for row in csv.DictReader(io.StringIO(resp.text)):
        name = (row.get("State Battalion") or "").strip()
        link = (row.get("Shop Link") or "").strip()
        if name and link:
            lookup[normalize_name(name)] = link
    return lookup


def attach_merch_links(states: list[dict], merch_by_state: dict) -> list[dict]:
    if not merch_by_state:
        return [{**s, "merch_url": None} for s in states]

    enriched = []
    for s in states:
        stripped = NAME_SUFFIX_RE.sub("", s["name"]).strip()
        key = normalize_name(stripped)
        enriched.append({**s, "merch_url": merch_by_state.get(key)})
    return enriched


def fetch_page(url: str = CAMPAIGN_URL) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_states(html: str) -> list[dict]:
    """
    Finds every anchor tag linking to a /campaigns/50forua-* state page and
    parses "<rank> <state name> $<amount>" out of its text.
    """
    soup = BeautifulSoup(html, "html.parser")
    states = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not CAMPAIGN_LINK_RE.search(href):
            continue
        if href in seen_urls:
            continue

        text = " ".join(a.get_text(separator=" ").split())
        m = STATE_LINE_RE.match(text)
        if not m:
            continue

        seen_urls.add(href)
        rank, name, amount_str = m.groups()
        states.append(
            {
                "rank": int(rank),
                "name": name.strip(),
                "amount": float(amount_str.replace(",", "")),
                "url": href if href.startswith("http") else f"https://car4ukraine.com{href}",
            }
        )

    states.sort(key=lambda s: s["rank"])
    return states


def parse_totals(html: str) -> dict:
    text = " ".join(BeautifulSoup(html, "html.parser").get_text(separator=" ").split())

    def _num(pattern, cast=float):
        m = pattern.search(text)
        return cast(m.group(1).replace(",", "")) if m else None

    return {
        "donated": _num(DONATED_RE, float),
        "donations_count": _num(DONATIONS_COUNT_RE, int),
        "goal": _num(GOAL_RE, float),
    }


def build_snapshot() -> dict:
    html = fetch_page()
    states = parse_states(html)
    totals = parse_totals(html)

    if not states:
        raise RuntimeError(
            "No state rankings were found on the page. The page structure may "
            "have changed and the scraper needs updating."
        )

    state_meta = load_state_meta()

    ledger_rows = fetch_vehicle_ledger(VEHICLE_LEDGER_URL)
    vehicles_by_state, help99_dollars_by_state = aggregate_vehicle_data(
        ledger_rows, set(state_meta.keys())
    )

    # Add any state that only ever fundraised via Help99 and never
    # appeared on car4ukraine.com at all, before attaching flags/vehicles
    # so those synthetic entries get the same treatment as everyone else.
    states = add_missing_help99_only_states(states, vehicles_by_state, help99_dollars_by_state, state_meta)

    states = attach_flags(states, state_meta)
    states = attach_vehicle_data(states, vehicles_by_state, help99_dollars_by_state)

    # Combine dollar totals (car4ukraine-scraped amount + Help99 ledger
    # amount) before ranking, since ranking's tie-break uses amount.
    states, total_help99_dollars = combine_help99_dollars(states, help99_dollars_by_state)

    states = rerank_by_vehicles(states)

    merch_by_state = fetch_merch_links(MERCH_SHEET_URL)
    states = attach_merch_links(states, merch_by_state)

    # The progress bar reflects the aggregate across all partners (not
    # just car4ukraine) - it's the overall 50 States for Ukraine total,
    # per Colin's direction. It's expected/fine for this to exceed
    # car4ukraine's own displayed total, since Help99 is a separate
    # contributing partner.
    if totals.get("donated") is not None and total_help99_dollars:
        totals["donated"] = round(totals["donated"] + total_help99_dollars, 2)

    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": CAMPAIGN_URL,
        "schema_version": SCHEMA_VERSION,
        "rank_version": RANK_VERSION,
        "totals": totals,
        "states": states,
    }


def append_history(snapshot: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(snapshot, separators=(",", ":")) + "\n")


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    snapshots = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                snapshots.append(json.loads(line))
    return snapshots


def find_snapshot_near(history: list[dict], target: datetime, tolerance_hours: float = 12) -> dict | None:
    """Finds the history entry closest to `target`, within tolerance_hours.
    Excludes snapshots with a schema_version older than current, since
    comparing amounts across that meaning-change would be misleading.
    This does NOT check rank_version - rank safety is checked separately,
    right where rank_improvement_7d is computed, so a rank-algorithm
    change never blocks dollar/vehicle delta calculations, which aren't
    affected by it."""
    best = None
    best_diff = None
    for snap in history:
        if snap.get("schema_version", 0) < SCHEMA_VERSION:
            continue
        ts = datetime.fromisoformat(snap["scraped_at"])
        diff = abs((ts - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best = snap
            best_diff = diff
    if best is not None and best_diff is not None and best_diff <= tolerance_hours * 3600:
        return best
    return None


def build_rankings_with_deltas(snapshot: dict, history: list[dict]) -> dict:
    now = datetime.fromisoformat(snapshot["scraped_at"])
    week_ago_target = now.timestamp() - 7 * 24 * 3600
    week_ago_target = datetime.fromtimestamp(week_ago_target, tz=timezone.utc)

    baseline = find_snapshot_near(history, week_ago_target, tolerance_hours=18)
    baseline_by_name = {s["name"]: s for s in baseline["states"]} if baseline else {}

    enriched_states = []
    for s in snapshot["states"]:
        prior = baseline_by_name.get(s["name"])

        delta = None
        if prior is not None:
            delta = round(s["amount"] - prior["amount"], 2)

        vehicles_delta = None
        prior_vehicles = prior.get("vehicles_delivered") if prior else None
        if prior_vehicles is not None:
            current_vehicles = s.get("vehicles_delivered", 0.0)
            vehicles_delta = round(current_vehicles - prior_vehicles, 4)

        # Rank movement (higher rank number = worse, so "improved" means the
        # number went down - a prior rank of 5 -> current rank of 2 is a
        # +3 improvement). Only shown when positive (moved up); staying
        # the same or dropping shows nothing, matching how negative dollar
        # deltas are also hidden rather than shown discouragingly.
        # prior_rank can be None even when `prior` exists - e.g. a
        # Help99-only state added by the one-time backfill script never
        # had a real computed rank in that old snapshot. That's fine and
        # self-resolving: it just means no rank-movement badge for that
        # state until enough newly-computed history accumulates.
        rank_improvement = None
        prior_rank = prior.get("rank") if prior else None
        current_rank = s.get("rank")
        prior_rank_version = prior.get("rank_version", 0) if prior else 0
        if prior_rank is not None and current_rank is not None and prior_rank_version >= RANK_VERSION:
            diff = prior_rank - current_rank
            if diff > 0:
                rank_improvement = diff

        enriched_states.append({
            **s,
            "amount_7d_ago": prior["amount"] if prior else None,
            "delta_7d": delta,
            "vehicles_delivered_7d_ago": prior_vehicles,
            "vehicles_delta_7d": vehicles_delta,
            "rank_improvement_7d": rank_improvement,
        })

    movers = [s for s in enriched_states if s["delta_7d"] is not None]
    top_movers = sorted(movers, key=lambda s: s["delta_7d"], reverse=True)

    return {
        "scraped_at": snapshot["scraped_at"],
        "source_url": snapshot["source_url"],
        "totals": snapshot["totals"],
        "baseline_date": baseline["scraped_at"] if baseline else None,
        "states": enriched_states,
        "top_movers_7d": top_movers[:10],
    }


def main() -> int:
    try:
        snapshot = build_snapshot()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    append_history(snapshot)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)

    history = load_history()
    rankings = build_rankings_with_deltas(snapshot, history)
    with open(RANKINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(rankings, f, indent=2)

    print(f"OK: {len(snapshot['states'])} states, total donated = {snapshot['totals']['donated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
