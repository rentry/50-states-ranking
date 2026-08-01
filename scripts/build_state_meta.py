#!/usr/bin/env python3
"""
One-time (not part of the hourly scrape) helper that:
1. Copies flag SVGs from the source repo into assets/flags/ with clean,
   predictable filenames (e.g. "ohio.svg", "district-of-columbia.svg").
2. Builds state_meta.json: a static lookup table mapping normalized state
   names/aliases to their flag file, used by scrape_rankings.py to attach
   flag_url to each ranking entry without any extra network calls.

Run this again only if car4ukraine.com adds a state/territory this repo
doesn't already cover.
"""

import json
import re
import shutil
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parent / "us-state-flags-svg" / "flags"
DEST_DIR = Path(__file__).resolve().parent.parent / "assets" / "flags"
META_FILE = Path(__file__).resolve().parent.parent / "state_meta.json"

# canonical_name -> (source SVG filename, list of extra aliases beyond the
# canonical name itself and its standard postal abbreviation)
STATES = {
    "Alabama": ("Flag_of_Alabama.svg", []),
    "Alaska": ("Flag_of_Alaska.svg", []),
    "American Samoa": ("Flag_of_American_Samoa.svg", []),
    "Arizona": ("Flag_of_Arizona.svg", []),
    "Arkansas": ("Flag_of_Arkansas.svg", []),
    "California": ("Flag_of_California.svg", []),
    "Colorado": ("Flag_of_Colorado_designed_by_Andrew_Carlisle_Carson.svg", []),
    "Connecticut": ("Flag_of_Connecticut.svg", []),
    "Delaware": ("Flag_of_Delaware.svg", []),
    "District of Columbia": ("Flag_of_the_District_of_Columbia.svg", ["dc", "washington dc", "washington d.c."]),
    "Florida": ("Flag_of_Florida.svg", []),
    "Georgia": ("Flag_of_Georgia_(U.S._state).svg", []),
    "Guam": ("Flag_of_Guam.svg", []),
    "Hawaii": ("Flag_of_Hawaii.svg", []),
    "Idaho": ("Flag_of_Idaho.svg", []),
    "Illinois": ("Flag_of_Illinois.svg", []),
    "Indiana": ("Flag_of_Indiana.svg", []),
    "Iowa": ("Flag_of_Iowa.svg", []),
    "Kansas": ("Flag_of_Kansas.svg", []),
    "Kentucky": ("Flag_of_Kentucky.svg", []),
    "Louisiana": ("Flag_of_Louisiana.svg", []),
    "Maine": ("Flag_of_Maine.svg", []),
    "Maryland": ("Flag_of_Maryland.svg", []),
    "Massachusetts": ("Flag_of_Massachusetts.svg", []),
    "Michigan": ("Flag_of_Michigan.svg", []),
    "Minnesota": ("Flag_of_Minnesota.svg", []),
    "Mississippi": ("Flag_of_Mississippi.svg", []),
    "Missouri": ("Flag_of_Missouri.svg", []),
    "Montana": ("Flag_of_Montana.svg", []),
    "Nebraska": ("Flag_of_Nebraska.svg", []),
    "Nevada": ("Flag_of_Nevada.svg", []),
    "New Hampshire": ("Flag_of_New_Hampshire.svg", []),
    "New Jersey": ("Flag_of_New_Jersey.svg", []),
    "New Mexico": ("Flag_of_New_Mexico.svg", []),
    "New York": ("Flag_of_New_York.svg", []),
    "North Carolina": ("Flag_of_North_Carolina.svg", ["nc"]),
    "North Dakota": ("Flag_of_North_Dakota.svg", ["nd"]),
    "Northern Mariana Islands": ("Flag_of_the_Northern_Mariana_Islands.svg", []),
    "Ohio": ("Flag_of_Ohio.svg", []),
    "Oklahoma": ("Flag_of_Oklahoma.svg", []),
    "Oregon": ("Flag_of_Oregon.svg", []),
    "Pennsylvania": ("Flag_of_Pennsylvania.svg", []),
    "Puerto Rico": ("Flag_of_Puerto_Rico.svg", []),
    "Rhode Island": ("Flag_of_Rhode_Island.svg", []),
    "South Carolina": ("Flag_of_South_Carolina.svg", ["sc"]),
    "South Dakota": ("Flag_of_South_Dakota.svg", ["sd"]),
    "Tennessee": ("Flag_of_Tennessee.svg", []),
    "Texas": ("Flag_of_Texas.svg", []),
    "Utah": ("Flag_of_Utah.svg", []),
    "Vermont": ("Flag_of_Vermont.svg", []),
    "Virginia": ("Flag_of_Virginia.svg", []),
    "Virgin Islands": ("Flag_of_the_United_States_Virgin_Islands.svg", ["us virgin islands"]),
    "Washington": ("Flag_of_Washington.svg", []),
    "West Virginia": ("Flag_of_West_Virginia.svg", ["wv"]),
    "Wisconsin": ("Flag_of_Wisconsin.svg", ["wi"]),
    "Wyoming": ("Flag_of_Wyoming.svg", []),
}


def slugify(name: str) -> str:
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace - used both for
    building lookup keys and for normalizing scraped display names."""
    s = text.lower().strip()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def main():
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    lookup = {}
    missing_sources = []

    for canonical, (src_filename, aliases) in STATES.items():
        src_path = SOURCE_DIR / src_filename
        if not src_path.exists():
            missing_sources.append((canonical, src_filename))
            continue

        slug = slugify(canonical)
        dest_filename = f"{slug}.svg"
        shutil.copyfile(src_path, DEST_DIR / dest_filename)

        keys = {normalize(canonical)} | {normalize(a) for a in aliases}
        for key in keys:
            lookup[key] = {
                "canonical_name": canonical,
                "flag_file": f"assets/flags/{dest_filename}",
            }

    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(lookup, f, indent=2, sort_keys=True)

    print(f"Wrote {len(STATES)} flags to {DEST_DIR}")
    print(f"Wrote {len(lookup)} lookup keys to {META_FILE}")
    if missing_sources:
        print("MISSING SOURCE FILES (not copied):")
        for canonical, fname in missing_sources:
            print(f"  {canonical} -> {fname}")


if __name__ == "__main__":
    main()
