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

DELIVERED_STATUS = "9 delivered"  # normalized (lowercased/stripped) match target
HELP99_PARTNER = "help99"

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


def aggregate_vehicle_data(rows: list[dict], known_state_keys: set) -> tuple[dict, dict]:
    """
    Aggregates the vehicle ledger into per-state vehicle counts (float,
    supports fractional/weighted credit) and a Help99-partner flag.
    Only rows with Status == "9 Delivered" are counted. Multi-state rows
    use "State Weights" if present, otherwise split equally. Names that
    don't match a known state (e.g. "Chicago") are skipped with a note,
    not an error.
    """
    vehicles_by_state = {}
    help99_by_state = {}

    for row in rows:
        status = (row.get("Status") or "").strip().lower()
        if status != DELIVERED_STATUS:
            continue

        battalion_field = (row.get("State Battalion") or "").strip()
        weights_field = (row.get("State Weights") or "").strip()
        is_help99 = (row.get("Partner") or "").strip().lower() == HELP99_PARTNER

        if weights_field:
            pairs = parse_state_weights(weights_field)
        else:
            names = [n.strip() for n in battalion_field.split(",") if n.strip()]
            weight = 1.0 / len(names) if names else 0
            pairs = [(n, weight) for n in names]

        for name, weight in pairs:
            key = normalize_name(name)
            if key not in known_state_keys:
                if key in KNOWN_NON_STATE_NAMES:
                    print(f"INFO: ignoring known non-state entry '{name}' (row: {row.get('Name', '?')})", file=sys.stderr)
                else:
                    gh_warning(
                        f"unrecognized name '{name}' in row '{row.get('Name', '?')}' - "
                        f"possible typo in the sheet? This state's vehicle credit was NOT counted."
                    )
                continue
            vehicles_by_state[key] = vehicles_by_state.get(key, 0.0) + weight
            if is_help99:
                help99_by_state[key] = True

    return vehicles_by_state, help99_by_state


def attach_vehicle_data(states: list[dict], vehicles_by_state: dict, help99_by_state: dict) -> list[dict]:
    enriched = []
    for s in states:
        stripped = NAME_SUFFIX_RE.sub("", s["name"]).strip()
        key = normalize_name(stripped)
        enriched.append({
            **s,
            "vehicles_delivered": round(vehicles_by_state.get(key, 0.0), 4),
            "includes_help99": help99_by_state.get(key, False),
        })
    return enriched


def rerank_by_vehicles(states: list[dict]) -> list[dict]:
    """
    Re-sorts and re-ranks states with vehicles_delivered as the primary
    key (descending) and amount as the tie-breaker for list order only.
    States tied on vehicles_delivered share the same rank number
    (competition ranking: e.g. 1, 1, 3 - not 1, 1, 2), even if their
    amounts differ. The original car4ukraine rank is preserved separately
    as fundraising_rank for reference.
    """
    ordered = sorted(states, key=lambda s: (-s.get("vehicles_delivered", 0.0), -s["amount"]))

    reranked = []
    current_rank = 0
    prev_vehicles = None
    for i, s in enumerate(ordered):
        vehicles = s.get("vehicles_delivered", 0.0)
        if vehicles != prev_vehicles:
            current_rank = i + 1
            prev_vehicles = vehicles
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
    states = attach_flags(states, state_meta)

    ledger_rows = fetch_vehicle_ledger(VEHICLE_LEDGER_URL)
    vehicles_by_state, help99_by_state = aggregate_vehicle_data(ledger_rows, set(state_meta.keys()))
    states = attach_vehicle_data(states, vehicles_by_state, help99_by_state)
    states = rerank_by_vehicles(states)

    return {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source_url": CAMPAIGN_URL,
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
    """Finds the history entry closest to `target`, within tolerance_hours."""
    best = None
    best_diff = None
    for snap in history:
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
        enriched_states.append({**s, "amount_7d_ago": prior["amount"] if prior else None, "delta_7d": delta})

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
