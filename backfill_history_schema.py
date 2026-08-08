#!/usr/bin/env python3
"""
ONE-TIME migration script - not part of the regular hourly scrape.

Patches old entries in data/history.jsonl (recorded before the Help99
dollar-combining feature existed) by adding each state's static Help99
dollar contribution to its historical `amount`, and adding synthetic
entries for Help99-only states (e.g. New York, Massachusetts) that
didn't exist in car4ukraine-derived snapshots at the time.

This is safe specifically because Help99/NAFO fundraising is frozen -
per Colin, no further money will be raised through that partner, so
today's Help99 dollar total per state is also the correct historical
total at any earlier point in time. We're not guessing at old data,
we're applying a constant that was always true.

Vehicle counts are intentionally NOT backfilled here - the rules for
which statuses count as "delivered" changed over time (e.g. "8 Deployed"
was added partway through), so old vehicle counts aren't as cleanly
reconcilable, and vehicle delta isn't currently displayed in the widget
anyway. Only dollar amounts get patched.

After running this, commit the updated data/history.jsonl. The next
scrape run's delta calculation will then have valid, comparable
baselines immediately - no need to wait another 7 days.

Usage:
    python3 backfill_history_schema.py
"""

import json
import sys
from pathlib import Path

import scrape_rankings as sr

HISTORY_FILE = Path(__file__).resolve().parent / "data" / "history.jsonl"


def main() -> int:
    if not HISTORY_FILE.exists():
        print(f"ERROR: {HISTORY_FILE} not found.", file=sys.stderr)
        return 1

    print("Fetching current vehicle ledger to get static Help99 dollar totals...")
    state_meta = sr.load_state_meta()
    ledger_rows = sr.fetch_vehicle_ledger(sr.VEHICLE_LEDGER_URL)
    vehicles_by_state, help99_dollars_by_state = sr.aggregate_vehicle_data(
        ledger_rows, set(state_meta.keys())
    )

    if not help99_dollars_by_state:
        print("ERROR: no Help99 dollar data found - aborting without changes.", file=sys.stderr)
        return 1

    print(f"Found Help99 dollar totals for {len(help99_dollars_by_state)} state(s):")
    for key, amount in sorted(help99_dollars_by_state.items(), key=lambda x: -x[1]):
        print(f"  {key}: ${amount:,.2f}")

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    patched_count = 0
    skipped_count = 0
    output_lines = []

    for line in lines:
        snap = json.loads(line)

        needs_amount_fix = snap.get("schema_version", 0) < sr.SCHEMA_VERSION
        needs_rank_fix = not snap.get("rank_corrected", False)

        if not needs_amount_fix and not needs_rank_fix:
            skipped_count += 1
            output_lines.append(json.dumps(snap, separators=(",", ":")))
            continue

        existing_keys = set()
        for s in snap["states"]:
            stripped = sr.NAME_SUFFIX_RE.sub("", s["name"]).strip()
            existing_keys.add(sr.normalize_name(stripped))

        if needs_amount_fix:
            patched_states = []
            for s in snap["states"]:
                stripped = sr.NAME_SUFFIX_RE.sub("", s["name"]).strip()
                key = sr.normalize_name(stripped)
                help99_amount = help99_dollars_by_state.get(key, 0.0)
                patched_states.append({**s, "amount": round(s["amount"] + help99_amount, 2)})

            missing_keys = set(help99_dollars_by_state.keys()) - existing_keys
            for key in sorted(missing_keys):
                meta_entry = state_meta.get(key)
                canonical_name = meta_entry["canonical_name"] if meta_entry else key.title()
                patched_states.append({
                    "rank": None,
                    "name": canonical_name,
                    "amount": round(help99_dollars_by_state[key], 2),
                    "url": None,
                    "fundraising_rank": None,
                })
        else:
            patched_states = snap["states"]

        # Recompute rank using the now-corrected amounts. The original
        # rank stored in this old entry was computed with the pre-fix
        # (car4ukraine-only) tiebreak amounts - leaving it as-is would
        # make rank-movement comparisons vulnerable to the same class of
        # false-jump bug we just fixed for dollar deltas, since ties get
        # broken by amount.
        patched_states = sr.rerank_by_vehicles(patched_states)
        snap["states"] = patched_states
        snap["schema_version"] = sr.SCHEMA_VERSION
        snap["rank_corrected"] = True
        patched_count += 1
        output_lines.append(json.dumps(snap, separators=(",", ":")))

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(output_lines) + "\n")

    print(f"\nDone. Patched {patched_count} old entries, left {skipped_count} already-current entries unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
