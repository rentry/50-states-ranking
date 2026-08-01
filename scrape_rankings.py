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

# Set this once you know your GitHub Pages URL, e.g.
# "https://your-username.github.io/50-states-ranking"
# If left blank, flag_url in the output will be a relative path instead
# of a full URL (e.g. "assets/flags/ohio.svg").
PAGES_BASE_URL = "https://rentry.github.io/50-states-ranking"

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
