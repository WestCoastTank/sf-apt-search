#!/usr/bin/env python3
"""
SF apartment refresh — pulls from Craigslist / Zillow / Padmapper, merges into
data/listings.json, marks vanished listings inactive, appends a refresh log row.

Craigslist is pulled live from RSS (no browser).  Zillow + Padmapper are pulled
by the agent via Claude in Chrome and dropped as JSON files in data/pulls/;
this script reads the most recent pull per source and merges everything.

Usage:
    python refresh.py
    python refresh.py --no-cl                # skip craigslist
    python refresh.py --max-pull-age-hours 24
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ----------------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PULLS_DIR = DATA_DIR / "pulls"
LISTINGS_PATH = DATA_DIR / "listings.json"
GEOCODE_CACHE_PATH = DATA_DIR / "geocode_cache.json"

DATA_DIR.mkdir(exist_ok=True)
PULLS_DIR.mkdir(exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 sf-apartment-search/1.0"

# ----------------------------------------------------------------------------
# Config — neighborhoods, corridors, hard requirements, weights
# ----------------------------------------------------------------------------

# Approximate bounding boxes (smaller / more specific neighborhoods first so
# Duboce Triangle wins over Hayes Valley when they overlap).
NEIGHBORHOOD_BOXES: list[tuple[str, list[tuple[float, float, float, float]]]] = [
    # Smaller / more specific neighborhoods first so they win when areas overlap.
    ("Duboce Triangle", [(37.7665, 37.7700, -122.4360, -122.4290)]),
    ("Cole Valley",     [(37.7625, 37.7700, -122.4570, -122.4460)]),
    # Castro covers the broad commercial/residential zone (17th-22nd, Market to Diamond).
    # Diamond St addresses, Hartford, Sanchez, Castro itself — all classify here.
    # Note: in everyday SF usage "Castro" and "Eureka Valley" are mostly synonymous;
    # we keep both as separate buckets so the user can filter by either, but Castro
    # gets first crack at any address that could be called either.
    ("Castro",          [(37.7575, 37.7665, -122.4410, -122.4275)]),
    # Eureka Valley narrowed to the western edge near Diamond Heights / 22nd-Caselli area.
    ("Eureka Valley",   [(37.7575, 37.7625, -122.4470, -122.4410)]),
    ("NoPa",            [(37.7700, 37.7800, -122.4520, -122.4360)]),
    ("Hayes Valley",    [(37.7720, 37.7800, -122.4360, -122.4180)]),
    ("Noe Valley",      [(37.7440, 37.7570, -122.4400, -122.4225)]),
    ("Mission",         [(37.7470, 37.7700, -122.4275, -122.4030)]),
]

ALLOWED_NEIGHBORHOODS = {n for n, _ in NEIGHBORHOOD_BOXES}

# Geographic inclusion: any listing within 1.5 miles of 94114 (Castro centroid)
# is considered in-area. Listings still get a specific neighborhood label
# (Castro, Mission, etc.) for display, but the in-area test uses radius.
TARGET_CENTROID = (37.7615, -122.4350)  # 94114
MAX_RADIUS_MILES = 1.75

# Major BART/Muni stations near 94114 — used for transit_score
TRANSIT_STATIONS = [
    ("Castro Muni",       37.7615, -122.4350),
    ("Church St Muni",    37.7666, -122.4287),
    ("16th St Mission BART", 37.7651, -122.4196),
    ("24th St Mission BART", 37.7522, -122.4188),
    ("Van Ness Muni",     37.7752, -122.4188),
    ("Civic Center BART", 37.7795, -122.4140),
    ("Powell BART",       37.7843, -122.4080),
    ("Forest Hill Muni",  37.7470, -122.4600),
    ("Glen Park BART",    37.7335, -122.4341),
]

def miles_between(lat1, lng1, lat2, lng2):
    dx = (lng1 - lng2) * 54.6
    dy = (lat1 - lat2) * 69
    return (dx * dx + dy * dy) ** 0.5

def nearest_transit_miles(lat, lng):
    if lat is None or lng is None: return None
    return min(miles_between(lat, lng, slat, slng) for _, slat, slng in TRANSIT_STATIONS)

def miles_from_target(lat, lng):
    """Approximate distance in miles at SF latitude (1° lat ≈ 69mi, 1° lng ≈ 54.6mi)."""
    if lat is None or lng is None:
        return None
    dx = (lng - TARGET_CENTROID[1]) * 54.6
    dy = (lat - TARGET_CENTROID[0]) * 69
    return (dx * dx + dy * dy) ** 0.5

def is_within_radius(lat, lng, miles=MAX_RADIUS_MILES):
    d = miles_from_target(lat, lng)
    return d is not None and d <= miles

# Adjacent neighborhoods — within ~5 blocks of the 8 targets. Listings here go
# to a separate "Adjacent" tab rather than being silently dropped.
ADJACENT_BOXES: list[tuple[str, list[tuple[float, float, float, float]]]] = [
    ("Lower Haight",          [(37.7700, 37.7745, -122.4400, -122.4290)]),
    ("Buena Vista / Ashbury", [(37.7665, 37.7710, -122.4470, -122.4380)]),
    ("Inner Sunset",          [(37.7580, 37.7660, -122.4720, -122.4570)]),
    ("Bernal Heights",        [(37.7370, 37.7470, -122.4275, -122.4030)]),
    ("Western Addition",      [(37.7770, 37.7850, -122.4400, -122.4280)]),
    ("Glen Park",              [(37.7340, 37.7440, -122.4360, -122.4220)]),
    ("Potrero Hill",          [(37.7530, 37.7670, -122.4030, -122.3900)]),
    ("Civic Center",          [(37.7770, 37.7820, -122.4250, -122.4150)]),
]

ALL_NHS_BOXES = NEIGHBORHOOD_BOXES + ADJACENT_BOXES
ADJACENT_NEIGHBORHOODS = {n for n, _ in ADJACENT_BOXES}

# Main commercial corridors — listings on these get side_street=False.
MAIN_CORRIDORS = {
    "Market St", "Divisadero St", "Mission St", "Castro St", "Valencia St",
    "Octavia Blvd", "Fell St", "Oak St", "Van Ness Ave", "Church St", "24th St",
}

DEFAULT_WEIGHTS = {
    # Bobby's dream: classic Victorian/Edwardian SFH, top floor, RC,
    # in-unit laundry, garage parking, side street near corridor, transit, outdoor.
    "price":        10,  # under cap good, way-under cap better
    "classic_sf":   15,  # Victorian/Edwardian/pre-war/SFH
    "rent_control": 15,
    "top_floor":    12,
    "laundry":      10,  # in_unit > in_building > none
    "parking":      10,  # garage/deeded > driveway > none
    "quiet_street":  10, # side street near a main corridor
    "transit":       8,  # distance to BART/Muni
    "outdoor":      10,
}

# Source precedence for cross-posted dedup
SOURCE_PRECEDENCE = {"zillow": 0, "padmapper": 1, "craigslist": 2}

# ----------------------------------------------------------------------------
# Utilities
# ----------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def load_json(p: Path, default: Any) -> Any:
    if not p.exists():
        return default
    with p.open() as f:
        return json.load(f)

def write_json(p: Path, obj: Any) -> None:
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    tmp.replace(p)

def normalize_address(addr: str | None) -> str:
    if not addr:
        return ""
    s = addr.lower().strip()
    s = re.sub(r"[#,.]", " ", s)
    s = re.sub(r"\bavenue\b", "ave", s)
    s = re.sub(r"\bstreet\b", "st", s)
    s = re.sub(r"\bboulevard\b", "blvd", s)
    s = re.sub(r"\bdrive\b", "dr", s)
    s = re.sub(r"\bapartment\b|\bapt\b", "", s)
    s = re.sub(r"\s+", " ", s)
    return s

def desc_fingerprint(text: str | None) -> str:
    if not text:
        return ""
    s = re.sub(r"\W+", " ", text.lower()).strip()
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]

def strip_html(text: str | None, max_len: int = 200) -> str:
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t

# ----------------------------------------------------------------------------
# Geocoding (Nominatim, cached)
# ----------------------------------------------------------------------------

_geocode_cache: dict[str, dict[str, float] | None] | None = None

def _load_geocode_cache() -> dict:
    global _geocode_cache
    if _geocode_cache is None:
        _geocode_cache = load_json(GEOCODE_CACHE_PATH, {})
    return _geocode_cache

def _save_geocode_cache() -> None:
    if _geocode_cache is not None:
        write_json(GEOCODE_CACHE_PATH, _geocode_cache)

def geocode(address: str) -> dict | None:
    """Return {'lat': ..., 'lng': ...} or None. Cached, rate-limited (1 req/sec)."""
    if not address:
        return None
    key = normalize_address(address)
    cache = _load_geocode_cache()
    if key in cache:
        return cache[key]

    try:
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
            "q": f"{address}, San Francisco, CA",
            "format": "json", "limit": 1,
        })
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        time.sleep(1.0)
        if data:
            result = {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"])}
        else:
            result = None
    except Exception as e:
        print(f"  geocode error for {address!r}: {e}", file=sys.stderr)
        result = None

    cache[key] = result
    _save_geocode_cache()
    return result

# ----------------------------------------------------------------------------
# Inference helpers
# ----------------------------------------------------------------------------

def classify_neighborhood(lat, lng, address, title, description) -> tuple[str | None, str]:
    """Returns (name, confidence). Tries target nbhds first, then adjacent ones."""
    if lat is not None and lng is not None:
        for name, boxes in ALL_NHS_BOXES:
            for lat_min, lat_max, lng_min, lng_max in boxes:
                if lat_min <= lat <= lat_max and lng_min <= lng <= lng_max:
                    return name, "high"
    text = " ".join(filter(None, [title, description])).lower()
    for name in ALLOWED_NEIGHBORHOODS | ADJACENT_NEIGHBORHOODS:
        if name.lower() in text:
            return name, "low"
    return None, "none"

def infer_side_street(address: str | None, title: str | None) -> bool | None:
    text = " ".join(filter(None, [address, title])).lower()
    if not text:
        return None
    for corridor in MAIN_CORRIDORS:
        if corridor.lower() in text:
            return False
    if address:  # we have an address but it's not a main corridor
        return True
    return None

# Description-keyword inference. Conservative: prefer "unknown" over wrong values.
LAUNDRY_PATTERNS = [
    # Negations FIRST — "no laundry" overrides any positive phrasing.
    ("none",        re.compile(r"\bno\s+laundry\b|\blaundromat[\s-]only\b|\bno\s+w/?d\b|\blaundromat\s+(?:nearby|down|across|around|\d+\s+block)", re.I)),
    ("in_unit",     re.compile(r"\b(in[\s-]unit|in[\s-]suite)\b.*\b(laundry|w/?d|washer)\b|\b(washer.{0,15}dryer|w/?d).{0,15}(in[\s-]unit|in[\s-]suite|in[\s-]apartment)\b", re.I)),
    ("in_unit",     re.compile(r"\bw/?d\s*in\s*unit\b|\bin[\s-]unit\s+w/?d\b|\bprivate\s+laundry\b", re.I)),
    ("in_building", re.compile(r"\b(shared|on[\s-]site|in[\s-]building|coin[\s-]op|building)\s+laundry\b|\blaundry\s+(in|on)\s+(building|site)\b", re.I)),
]

PARKING_PATTERNS = [
    ("garage",   re.compile(r"\b(garage|covered\s+parking|attached\s+parking)\b", re.I)),
    ("driveway", re.compile(r"\bdriveway\b|\bcarport\b", re.I)),
    ("deeded",   re.compile(r"\bdeeded\s+(parking|spot|garage)\b|\bassigned\s+(parking|spot|garage)\b|\bparking\s+spot\s+included\b|\bdedicated\s+(parking|garage|spot)\b", re.I)),
    ("street",   re.compile(r"\bstreet\s+parking\b|\bon[\s-]street\s+parking\b", re.I)),
    ("none",     re.compile(r"\bno\s+parking\b", re.I)),
]

def infer_laundry(text: str) -> str | None:
    if not text:
        return None
    for label, pat in LAUNDRY_PATTERNS:
        if pat.search(text):
            return label
    return None

def infer_parking(text: str) -> str | None:
    if not text:
        return None
    # Order matters: "garage" beats "street parking" if both present (likely garage included plus mentions of street)
    matches = []
    for label, pat in PARKING_PATTERNS:
        if pat.search(text):
            matches.append(label)
    # Prefer the most favorable match if both garage/deeded and street are mentioned
    for preferred in ("deeded", "garage", "driveway", "street", "none"):
        if preferred in matches:
            return preferred
    return None

TOP_FLOOR_RE = re.compile(r"\btop[\s-]floor\b|\bpenthouse\b|\bupper\s+unit\b", re.I)
NOT_TOP_RE   = re.compile(r"\bground[\s-]floor\b|\bgarden[\s-]level\b|\blower\s+unit\b", re.I)
OUTDOOR_RE   = re.compile(r"\b(deck|patio|yard|balcony|terrace|rooftop|backyard)\b", re.I)
DOG_OK_RE    = re.compile(r"\bdog[s]?\s+(ok|allowed|welcome|friendly)\b|\bpet[s]?\s+(ok|allowed|welcome)\b", re.I)
NO_PETS_RE   = re.compile(r"\bno\s+pets\b|\bno\s+dogs\b", re.I)
CATS_ONLY_RE = re.compile(r"\bcats?\s+(ok|only|allowed)\b(?!.*\bdogs?\s+(ok|allowed|welcome))", re.I)

def infer_top_floor(text: str) -> bool | None:
    if not text: return None
    if TOP_FLOOR_RE.search(text): return True
    if NOT_TOP_RE.search(text): return False
    return None

def infer_outdoor(text: str) -> bool | None:
    if not text: return None
    if OUTDOOR_RE.search(text): return True
    return None  # Absence isn't proof

def infer_pet_policy(text: str) -> tuple[str, bool | None]:
    """Returns (pet_policy, dog_friendly_bool)."""
    if not text: return "unstated", None
    if NO_PETS_RE.search(text): return "no_pets", False
    if DOG_OK_RE.search(text):  return "dog_ok", True
    if CATS_ONLY_RE.search(text): return "cats_only", False
    return "unstated", None

# Year-built / unit-count inference for RC
YEAR_BUILT_RE = re.compile(r"\b(?:built|constructed|year\s+built)[\s:]+(?:in\s+)?(19\d{2}|20\d{2})\b", re.I)
UNIT_COUNT_RE = re.compile(r"\b(\d+)[\s-]unit\s+building\b|\bunit\s+\d+\s+of\s+(\d+)\b", re.I)

def infer_year_built(text: str) -> int | None:
    if not text: return None
    m = YEAR_BUILT_RE.search(text)
    if m:
        try:
            return int(m.group(1))
        except: pass
    return None

def infer_unit_count(text: str) -> int | None:
    if not text: return None
    m = UNIT_COUNT_RE.search(text)
    if m:
        try:
            return int(next(g for g in m.groups() if g))
        except: pass
    return None

# ----------------------------------------------------------------------------
# Hard requirements
# ----------------------------------------------------------------------------

def check_hard_requirements(L: dict) -> tuple[bool, str | None]:
    if L.get("bedrooms", 0) < 2:
        return False, f"Bedrooms < 2 ({L.get('bedrooms')})"
    if L.get("price", 0) > 6500:
        return False, f"Price ${L['price']} > $6500"
    nh = L.get("neighborhood")
    # Use radius-based geo filter (1.5mi of 94114) as the primary inclusion check.
    lat, lng = L.get("lat"), L.get("lng")
    miles = miles_from_target(lat, lng)
    if miles is None:
        # No coords — fall back to polygon-classified nbhd
        if nh in ADJACENT_NEIGHBORHOODS:
            return False, f"__adjacent__:{nh}"
        if nh not in ALLOWED_NEIGHBORHOODS:
            return False, f"Outside target area (no coords, nbhd={nh!r})"
    elif miles > MAX_RADIUS_MILES:
        return False, f"{miles:.1f}mi from 94114 (>{MAX_RADIUS_MILES}mi)"
    # In-radius and has coords — still respect adjacency labels for the tab.
    elif nh in ADJACENT_NEIGHBORHOODS:
        return False, f"__adjacent__:{nh}"
    laundry = L.get("laundry")
    if laundry == "none":
        return False, "No laundry in unit or building (laundromat-only)"
    parking = L.get("parking")
    if parking == "street":
        return False, "No dedicated parking spot (street parking only)"
    if parking == "none":
        return False, "No parking"
    return True, None

# ----------------------------------------------------------------------------
# Rent-control inference
# ----------------------------------------------------------------------------

RC_LANGUAGE = re.compile(r"\b(rent[\s-]controlled|rent[\s-]stabilized)\b", re.I)
PRE_WAR     = re.compile(r"\b(edwardian|victorian|pre[\s-]war|1900s|1910s|1920s|vintage|classic\s+sf)\b", re.I)
NEW_BUILD   = re.compile(r"\b(new[\s-]construction|newly[\s-]built|brand[\s-]new|luxury\s+building)\b", re.I)

def infer_rc(L: dict) -> tuple[int, str]:
    desc = (L.get("description_snippet") or "") + " " + (L.get("title") or "")
    yr = L.get("year_built")
    units = L.get("unit_count") or 0
    if L.get("costa_hawkins_exempt"):
        return 0, "Costa-Hawkins exempt (SFH or condo)"
    if RC_LANGUAGE.search(desc):
        return 10, "Description explicitly mentions rent control."
    if NEW_BUILD.search(desc) or (yr and yr >= 1979):
        return 0, f"Built {yr} (post-1979) — not rent controlled." if yr else "New construction language — not rent controlled."
    if yr and yr < 1979 and units >= 2:
        return 10, f"Pre-1979 multi-unit ({units} units, built {yr})."
    if PRE_WAR.search(desc) and units >= 2:
        return 5, f"Pre-war language and {units}-unit building, but exact year not confirmed."
    if PRE_WAR.search(desc):
        return 5, "Pre-war language — multi-unit count not confirmed."
    return 0, "Insufficient evidence for rent control."

# ----------------------------------------------------------------------------
# Scoring (mirror of dashboard JS)
# ----------------------------------------------------------------------------

CLASSIC_SF_RE = re.compile(r"\b(edwardian|victorian|pre[\s-]war|1900s|1910s|1920s|vintage|classic\s+sf|sfh|single[\s-]family|townhouse|flat|bay\s+window|hardwood\s+floors?)\b", re.I)
NEW_BUILD_VETO_RE = re.compile(r"\b(adu|new\s+construction|newly[\s-]built|brand[\s-]new|new\s+adu|recently[\s-]built|2020|2021|2022|2023|2024|2025)\b", re.I)

def classic_sf_score(L: dict, weight: int) -> float:
    """High if listing reads like a classic SF Victorian/Edwardian flat or SFH.
    Veto if there's any signal of new construction (ADU, etc)."""
    text = (L.get("title") or "") + " " + (L.get("description_snippet") or "")
    yr = L.get("year_built")
    # Hard veto on new construction signals — overrides any classic match
    if NEW_BUILD_VETO_RE.search(text):
        return 0
    if yr and yr >= 1979:
        return 0
    if CLASSIC_SF_RE.search(text):
        return weight
    if yr and yr < 1940:
        return weight * 0.9
    if yr and yr < 1979:
        return weight * 0.5
    return 0

def score_listing(L: dict, w: dict[str, int] = DEFAULT_WEIGHTS) -> tuple[float, dict]:
    b = {}
    # Price fit — linear from $4k (full) to $6.5k (zero)
    if L["price"] <= 4000: b["price"] = w["price"]
    elif L["price"] >= 6500: b["price"] = 0
    else: b["price"] = ((6500 - L["price"]) / 2500) * w["price"]

    # Classic SF architecture
    b["classic_sf"] = classic_sf_score(L, w["classic_sf"])

    # Rent-controlled
    rc = L.get("likely_rent_controlled_score") or 0
    b["rent_control"] = w["rent_control"] if rc == 10 else w["rent_control"] * 0.5 if rc == 5 else 0

    # Top floor — strong boost when confirmed
    tf = L.get("top_floor")
    b["top_floor"] = w["top_floor"] if tf is True else 0 if tf is False else w["top_floor"] * 0.4

    # Laundry: in-unit wins
    laundry = L.get("laundry")
    b["laundry"] = (
        w["laundry"] if laundry == "in_unit"
        else w["laundry"] * 0.5 if laundry == "in_building"
        else w["laundry"] * 0.4 if laundry is None
        else 0
    )

    # Parking: garage/deeded ideal
    parking = L.get("parking")
    b["parking"] = (
        w["parking"] if parking in ("garage", "deeded")
        else w["parking"] * 0.7 if parking == "driveway"
        else w["parking"] * 0.4 if parking is None
        else 0
    )

    # Quiet side street near a corridor
    ss = L.get("side_street")
    b["quiet_street"] = w["quiet_street"] if ss is True else 0 if ss is False else w["quiet_street"] * 0.4

    # Transit access — distance to nearest BART/Muni
    transit_miles = nearest_transit_miles(L.get("lat"), L.get("lng"))
    if transit_miles is None:
        b["transit"] = w["transit"] * 0.4
    elif transit_miles <= 0.3:
        b["transit"] = w["transit"]
    elif transit_miles <= 0.5:
        b["transit"] = w["transit"] * 0.7
    elif transit_miles <= 0.8:
        b["transit"] = w["transit"] * 0.4
    else:
        b["transit"] = 0

    # Outdoor space
    b["outdoor"] = w["outdoor"] if L.get("outdoor_space") is True else 0

    return round(sum(b.values()), 1), b

# ----------------------------------------------------------------------------
# Craigslist RSS pull
# ----------------------------------------------------------------------------

CL_BASE = "https://sfbay.craigslist.org/search/sfc/apa"
CL_PARAMS_BASE = {
    "min_bedrooms": "2",
    "max_price": "6500",
    "availabilityMode": "0",
    "format": "rss",
}
CL_MAX_PAGES = 8         # 8 × 25 = 200 results max
CL_PAGE_SIZE = 25

# Title format: "$5500 / 2br - 1080ft2 - Sunny 2BR/2BA in Cole Valley w/ garage (cole valley)"
# Sometimes no sqft and/or no location. Tolerate both.
CL_TITLE_RE = re.compile(
    r"^\s*\$?(?P<price>[\d,]+)\s*/\s*(?P<beds>\d+)\s*br"
    r"(?:\s*-\s*(?P<sqft>\d+)\s*ft2?)?"
    r"\s*-\s*(?P<title>.+?)"
    r"(?:\s*\((?P<loc>[^)]+)\))?\s*$",
    re.I,
)

def cl_extract_id(link: str) -> str:
    """Craigslist URLs end in /<post-id>.html — that integer is the stable ID."""
    m = re.search(r"/(\d+)\.html", link or "")
    return f"cl-{m.group(1)}" if m else f"cl-{abs(hash(link))}"

def cl_parse_entry(entry, page_idx: int) -> dict | None:
    """Convert a feedparser entry → our normalized listing dict, or None to drop."""
    raw_title = entry.get("title") or ""
    m = CL_TITLE_RE.match(raw_title)
    if not m:
        return None  # skip entries we can't parse
    try:
        price = int(m.group("price").replace(",", ""))
        bedrooms = int(m.group("beds"))
    except (ValueError, TypeError):
        return None
    sqft = int(m.group("sqft")) if m.group("sqft") else None
    short_title = (m.group("title") or "").strip()
    location_hint = (m.group("loc") or "").strip()

    # geo
    lat = lng = None
    if entry.get("geo_lat") and entry.get("geo_long"):
        try: lat, lng = float(entry["geo_lat"]), float(entry["geo_long"])
        except: pass
    elif entry.get("georss_point"):
        try:
            parts = str(entry["georss_point"]).strip().split()
            lat, lng = float(parts[0]), float(parts[1])
        except: pass

    description_html = entry.get("summary") or entry.get("description") or ""
    description_text = strip_html(description_html, max_len=2000)
    snippet = strip_html(description_html, max_len=200)
    full_text = (raw_title + " " + description_text).lower()

    # photos
    photos = []
    for enc in entry.get("enclosures", []) or []:
        href = enc.get("href") or enc.get("url")
        if href and href.startswith("http"):
            photos.append(href)
    for media in entry.get("media_content", []) or []:
        href = media.get("url")
        if href and href.startswith("http") and href not in photos:
            photos.append(href)

    # Inferred fields
    nh, nh_conf = classify_neighborhood(lat, lng, location_hint, short_title, description_text)
    side = infer_side_street(location_hint, short_title)

    L = {
        "id": cl_extract_id(entry.get("link") or ""),
        "source": "craigslist",
        "source_url": entry.get("link") or "",
        "cross_posted_on": [],
        "title": short_title,
        "address": None,            # Craigslist rarely surfaces street address in RSS
        "neighborhood": nh,
        "neighborhood_confidence": nh_conf,
        "lat": lat,
        "lng": lng,
        "price": price,
        "bedrooms": bedrooms,
        "bathrooms": None,
        "sqft": sqft,
        "price_per_sqft": round(price / sqft, 2) if sqft else None,
        "laundry": infer_laundry(full_text),
        "parking": infer_parking(full_text),
        "top_floor": infer_top_floor(full_text),
        "outdoor_space": infer_outdoor(full_text),
        "side_street": side,
        "year_built": infer_year_built(full_text),
        "unit_count": infer_unit_count(full_text),
        "description_snippet": snippet,
        "date_posted": (entry.get("published") or entry.get("updated") or now_iso()),
        "photos": photos,
        "_desc_fingerprint": desc_fingerprint(description_text),
    }

    pet, dog = infer_pet_policy(full_text)
    L["pet_policy"] = pet
    L["dog_friendly"] = dog

    return L

def pull_craigslist(max_age_hours: float = 24) -> tuple[list[dict], list[dict]]:
    # Prefer browser-pulled cache file if recent (used when sandbox/CI can't reach craigslist.org).
    cached, _ = load_browser_pull("craigslist", max_age_hours)
    if cached:
        return cached, []

    try:
        import feedparser
    except ImportError:
        return [], [{"source": "craigslist", "message": "feedparser not installed; run: pip install feedparser"}]

    listings = []
    errors = []
    seen_ids = set()
    for page in range(CL_MAX_PAGES):
        params = dict(CL_PARAMS_BASE)
        if page > 0:
            params["s"] = str(page * CL_PAGE_SIZE)
        url = CL_BASE + "?" + urllib.parse.urlencode(params)
        try:
            feed = feedparser.parse(url, agent=USER_AGENT)
        except Exception as e:
            errors.append({"source": "craigslist", "message": f"page {page} fetch failed: {e}"})
            break
        if feed.bozo and not feed.entries:
            errors.append({"source": "craigslist", "message": f"page {page} parse error: {getattr(feed, 'bozo_exception', '?')}"})
            if page == 0:
                break  # nothing usable
        if not feed.entries:
            break  # ran out of pages
        new_this_page = 0
        for entry in feed.entries:
            L = cl_parse_entry(entry, page)
            if not L:
                continue
            if L["id"] in seen_ids:
                continue  # CL pagination sometimes overlaps
            seen_ids.add(L["id"])
            listings.append(L)
            new_this_page += 1
        if new_this_page == 0:
            break
        time.sleep(0.5)  # be polite between pages
    return listings, errors

# ----------------------------------------------------------------------------
# Detail-page fetcher (Zillow) — runs only for active listings scoring 60+
# that have null parking, per the spec.
# ----------------------------------------------------------------------------

DETAIL_CACHE_PATH = DATA_DIR / "detail_cache.json"

def _load_detail_cache() -> dict:
    return load_json(DETAIL_CACHE_PATH, {})

def _save_detail_cache(cache: dict) -> None:
    write_json(DETAIL_CACHE_PATH, cache)

# Patterns we look for in the full detail-page HTML
DETAIL_PARKING_PATS = [
    ("garage",   re.compile(r"\b(garage(?:\s+spaces?)?[:\s]+(?:1|2|3|on[\s-]site|attached|detached|covered)|covered\s+parking|attached\s+garage|garage\s+included)\b", re.I)),
    ("deeded",   re.compile(r"\b(deeded\s+(?:parking|spot|garage)|assigned\s+(?:parking|spot)|dedicated\s+parking)\b", re.I)),
    ("driveway", re.compile(r"\b(driveway|carport)\b", re.I)),
    ("street",   re.compile(r"\b(street\s+parking\s+only|on[\s-]street\s+parking)\b", re.I)),
    ("none",     re.compile(r"\b(parking[:\s]+none|no\s+parking)\b", re.I)),
]
DETAIL_LAUNDRY_PATS = [
    ("none",        re.compile(r"\b(no\s+laundry|laundry[:\s]+none|laundromat[\s-]only)\b", re.I)),
    ("in_unit",     re.compile(r"\b(in[\s-]unit\s+(?:laundry|w/?d|washer)|washer.{0,15}dryer.{0,15}in[\s-]unit|laundry[:\s]+in\s+unit)\b", re.I)),
    ("in_building", re.compile(r"\b(shared\s+laundry|in[\s-]building\s+laundry|laundry\s+(?:in\s+)?building|on[\s-]site\s+laundry|coin[\s-]op\s+laundry|laundry[:\s]+in\s+building)\b", re.I)),
]
DETAIL_YEAR_RE = re.compile(r"(?:built|year\s+built)[\s:]+(?:in\s+)?(19\d{2}|20\d{2})", re.I)
DETAIL_TOPFLOOR_RE = re.compile(r"\b(top[\s-]floor|penthouse)\b", re.I)
DETAIL_OUTDOOR_RE  = re.compile(r"\b(private\s+(?:deck|patio|balcony|yard|terrace)|own\s+(?:deck|patio|balcony))\b", re.I)
DETAIL_DOG_RE      = re.compile(r"\b(dogs?\s+(?:ok|allowed|welcome|friendly)|pets?\s+(?:ok|allowed|welcome))\b", re.I)
DETAIL_NOPETS_RE   = re.compile(r"\b(no\s+pets?|no\s+dogs?)\b", re.I)
DETAIL_CATSONLY_RE = re.compile(r"\bcats?\s+only\b", re.I)

def parse_detail_html(text: str) -> dict:
    """Best-effort extraction from any listing detail page HTML."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text))
    out = {}
    for label, pat in DETAIL_PARKING_PATS:
        if pat.search(text): out["parking"] = label; break
    for label, pat in DETAIL_LAUNDRY_PATS:
        if pat.search(text): out["laundry"] = label; break
    m = DETAIL_YEAR_RE.search(text)
    if m:
        try: out["year_built"] = int(m.group(1))
        except: pass
    if DETAIL_TOPFLOOR_RE.search(text): out["top_floor"] = True
    if DETAIL_OUTDOOR_RE.search(text):  out["outdoor_space"] = True
    if DETAIL_NOPETS_RE.search(text):   out["pet_policy"] = "no_pets"; out["dog_friendly"] = False
    elif DETAIL_DOG_RE.search(text):    out["pet_policy"] = "dog_ok"; out["dog_friendly"] = True
    elif DETAIL_CATSONLY_RE.search(text): out["pet_policy"] = "cats_only"; out["dog_friendly"] = False
    return out

def fetch_detail_page(url: str) -> str | None:
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  detail fetch error for {url}: {e}", file=sys.stderr)
        return None

def enrich_with_detail_pages(listings: list[dict]) -> int:
    """For active listings scoring 40+ with null parking/laundry, fetch detail page once each.
    Returns the number of listings enriched."""
    cache = _load_detail_cache()
    enriched = 0
    for L in listings:
        if L.get("status") not in ("active", "adjacent"): continue
        if (L.get("score") or 0) < 40: continue
        if L.get("parking") is not None and L.get("laundry") is not None: continue
        url = L.get("source_url")
        if not url: continue
        if url in cache:
            details = cache[url]
        else:
            html_text = fetch_detail_page(url)
            if not html_text:
                cache[url] = {}; continue
            details = parse_detail_html(html_text)
            cache[url] = details
            time.sleep(2.0)  # be polite
        # Only fill fields the listing is missing
        for k, v in details.items():
            if L.get(k) is None and v is not None:
                L[k] = v
        if details: enriched += 1
    _save_detail_cache(cache)
    return enriched

# ----------------------------------------------------------------------------
# LLM classification (optional, opt-in via --llm flag)
# ----------------------------------------------------------------------------

def llm_classify(listings: list[dict]) -> int:
    """Use Anthropic's Claude to classify pet_policy / outdoor / top_floor /
    side_street more accurately than the regex inference. Requires
    ANTHROPIC_API_KEY env var. Caches by description fingerprint."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("  LLM: ANTHROPIC_API_KEY not set, skipping", file=sys.stderr)
        return 0
    try:
        from anthropic import Anthropic
    except ImportError:
        print("  LLM: 'anthropic' package not installed; pip install anthropic", file=sys.stderr)
        return 0

    cache_path = DATA_DIR / "llm_cache.json"
    cache = load_json(cache_path, {})
    client = Anthropic(api_key=api_key)
    enriched = 0
    for L in listings:
        if L.get("status") != "active": continue
        fp = desc_fingerprint(L.get("description_snippet"))
        if not fp: continue
        if fp in cache:
            classification = cache[fp]
        else:
            prompt = f"""Classify this apartment listing. Reply ONLY with JSON, no prose.
Title: {L.get('title','')}
Description: {L.get('description_snippet','')}

Reply with this exact schema:
{{"pet_policy": "dog_ok"|"cats_only"|"no_pets"|"unstated",
  "outdoor_space": true|false|null,
  "top_floor": true|false|null,
  "laundry": "in_unit"|"in_building"|"none"|null,
  "parking": "garage"|"deeded"|"driveway"|"street"|"none"|null}}

Use null when the listing genuinely doesn't say. Do NOT guess."""
            try:
                msg = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                raw = msg.content[0].text.strip()
                # Extract JSON from response
                m = re.search(r"\{[^}]+\}", raw, re.S)
                classification = json.loads(m.group(0)) if m else {}
                cache[fp] = classification
            except Exception as e:
                print(f"  LLM error for {L.get('id')}: {e}", file=sys.stderr)
                continue
        # Only fill fields that aren't already known to a higher confidence
        for k in ("pet_policy", "outdoor_space", "top_floor", "laundry", "parking"):
            if classification.get(k) is not None and L.get(k) is None:
                L[k] = classification[k]
        if classification: enriched += 1
    write_json(cache_path, cache)
    return enriched

# ----------------------------------------------------------------------------
# Browser-source loaders (read from data/pulls/)
# ----------------------------------------------------------------------------

def load_browser_pull(source: str, max_age_hours: float) -> tuple[list[dict], list[dict]]:
    """Read the most recent data/pulls/<source>_*.json that's still fresh."""
    candidates = sorted(PULLS_DIR.glob(f"{source}_*.json"), reverse=True)
    if not candidates:
        return [], [{"source": source, "message": f"No {source} pull file found in data/pulls/. Ask Claude to refresh {source.title()} via Claude in Chrome."}]
    newest = candidates[0]
    age_hours = (time.time() - newest.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return [], [{"source": source, "message": f"{source} pull is {age_hours:.1f}h old (max {max_age_hours}h). Ask Claude to re-pull {source.title()}."}]
    try:
        data = json.loads(newest.read_text())
        return data.get("listings", []), []
    except Exception as e:
        return [], [{"source": source, "message": f"Failed to read {newest.name}: {e}"}]

def pull_zillow(max_age_hours: float = 24) -> tuple[list[dict], list[dict]]:
    return load_browser_pull("zillow", max_age_hours)

def pull_apartments(max_age_hours: float = 24) -> tuple[list[dict], list[dict]]:
    return load_browser_pull("apartments", max_age_hours)

def pull_padmapper(max_age_hours: float = 24) -> tuple[list[dict], list[dict]]:
    return load_browser_pull("padmapper", max_age_hours)

# ----------------------------------------------------------------------------
# Dedup / merge
# ----------------------------------------------------------------------------

def dedupe(listings: list[dict]) -> list[dict]:
    """Cross-source dedup. Strong match (address+beds+price ±$50) collapses; if
    address is missing, fall back to lat/lng-rounded match."""
    by_key: dict[tuple, dict] = {}
    for L in listings:
        if L.get("address"):
            key = ("addr", normalize_address(L["address"]), L.get("bedrooms"), round(L.get("price", 0) / 50) * 50)
        elif L.get("lat") is not None and L.get("lng") is not None:
            key = ("geo", round(L["lat"], 4), round(L["lng"], 4), L.get("bedrooms"), round(L.get("price", 0) / 50) * 50)
        else:
            key = ("id", L["id"])
        if key in by_key:
            existing = by_key[key]
            if SOURCE_PRECEDENCE.get(L["source"], 9) < SOURCE_PRECEDENCE.get(existing["source"], 9):
                cross = sorted(set(existing.get("cross_posted_on", []) + [existing["source"]]))
                L["cross_posted_on"] = cross
                # Preserve photos union
                L["photos"] = list(dict.fromkeys((L.get("photos") or []) + (existing.get("photos") or [])))
                by_key[key] = L
            else:
                cross = sorted(set(existing.get("cross_posted_on", []) + [L["source"]]))
                existing["cross_posted_on"] = cross
                existing["photos"] = list(dict.fromkeys((existing.get("photos") or []) + (L.get("photos") or [])))
        else:
            by_key[key] = L
    return list(by_key.values())

# ----------------------------------------------------------------------------
# Merge against existing listings.json
# ----------------------------------------------------------------------------

def merge(existing_doc: dict, fresh: list[dict]) -> dict:
    now = now_iso()
    previous_refresh = existing_doc.get("last_refresh")
    by_id = {L["id"]: L for L in existing_doc.get("listings", [])}
    fresh_ids = set()

    for f in fresh:
        # Geocode if we have an address but no coords
        if f.get("address") and (f.get("lat") is None or f.get("lng") is None):
            g = geocode(f["address"])
            if g:
                f["lat"] = g["lat"]
                f["lng"] = g["lng"]
        # Re-classify neighborhood if missing
        if not f.get("neighborhood"):
            nh, nh_conf = classify_neighborhood(f.get("lat"), f.get("lng"),
                                                f.get("address"), f.get("title"),
                                                f.get("description_snippet"))
            f["neighborhood"] = nh
            f["neighborhood_confidence"] = nh_conf
        # Re-infer side_street if address is now known and side_street wasn't set
        if f.get("side_street") is None and f.get("address"):
            f["side_street"] = infer_side_street(f.get("address"), f.get("title"))

        passes, reason = check_hard_requirements(f)
        if reason and reason.startswith("__adjacent__:"):
            f["status"] = "adjacent"
            f["exclusion_reason"] = None
        else:
            f["status"] = "active" if passes else "excluded"
            f["exclusion_reason"] = reason
        rc_score, rc_reason = infer_rc(f)
        f["likely_rent_controlled_score"] = rc_score
        f["rc_reasoning"] = rc_reason
        f["score"], f["score_breakdown"] = score_listing(f)
        f["date_last_seen"] = now

        fid = f["id"]
        fresh_ids.add(fid)
        if fid in by_id:
            old = by_id[fid]
            f["date_first_seen"] = old.get("date_first_seen", now)
            f["times_seen"] = (old.get("times_seen", 0)) + 1
            f["is_new_since_last_refresh"] = False
            # Keep stale photos if fresh has none
            if not f.get("photos") and old.get("photos"):
                f["photos"] = old["photos"]
            # Price history — only append when price changes
            ph = old.get("price_history") or [{"date": old.get("date_first_seen", now), "price": old.get("price")}]
            if f.get("price") != old.get("price"):
                ph.append({"date": now, "price": f.get("price")})
            f["price_history"] = ph
            f["previous_price"] = old.get("price")  # for the price-change badge
            # Preserve user-set fields not present in fresh
            for k in ("notes", "user_status"):
                if k not in f and old.get(k) is not None:
                    f[k] = old[k]
            by_id[fid] = f
        else:
            f["date_first_seen"] = now
            f["times_seen"] = 1
            f["is_new_since_last_refresh"] = True
            f["price_history"] = [{"date": now, "price": f.get("price")}]
            f["previous_price"] = None
            by_id[fid] = f

    newly_inactive = 0
    for L in by_id.values():
        if L["id"] not in fresh_ids and L.get("status") == "active":
            L["status"] = "inactive"
            newly_inactive += 1
        if L["id"] not in fresh_ids:
            L["is_new_since_last_refresh"] = False

    return {
        "version": "1.0",
        "last_refresh": now,
        "previous_refresh": previous_refresh,
        "listings": list(by_id.values()),
        "refresh_log": existing_doc.get("refresh_log", []),
        "_stats": {"newly_inactive": newly_inactive, "fresh_ids": fresh_ids},
    }

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def macos_notify(title: str, body: str) -> None:
    """Send a macOS notification (no-op on other platforms)."""
    if sys.platform != "darwin":
        return
    try:
        # Escape double-quotes in user-supplied strings
        body = body.replace('"', "'")
        title = title.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{body}" with title "{title}"'],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass

def run_refresh(args) -> dict:
    """Pure refresh, returns the log_entry dict. Reusable from CLI or HTTP."""
    started = time.time()
    cl, cl_err = ([], []) if args.no_cl else pull_craigslist()
    zw, zw_err = ([], []) if args.no_zw else pull_zillow(args.max_pull_age_hours)
    ap, ap_err = ([], []) if args.no_ap else pull_apartments(args.max_pull_age_hours)
    pm, pm_err = ([], []) if not args.pm else pull_padmapper(args.max_pull_age_hours)

    pulled = dedupe(cl + zw + ap + pm)
    existing = load_json(LISTINGS_PATH, {"version": "1.0", "listings": [], "refresh_log": []})
    merged = merge(existing, pulled)
    stats = merged.pop("_stats")

    # Optional enrichment passes — opt-in via flags. These run AFTER the merge
    # so they can target the now-current active set.
    enriched_detail = 0
    enriched_llm = 0
    if not getattr(args, "no_detail", False):
        enriched_detail = enrich_with_detail_pages(merged["listings"])
    if getattr(args, "llm", False):
        enriched_llm = llm_classify(merged["listings"])
    # Re-score after enrichment in case fields changed
    if enriched_detail or enriched_llm:
        for L in merged["listings"]:
            if L.get("status") == "active":
                L["score"], L["score_breakdown"] = score_listing(L)

    # Compute median $/sqft per neighborhood, set below_market flag, hidden_gem flag.
    by_nh: dict[str, list[float]] = {}
    for L in merged["listings"]:
        if L.get("status") not in ("active", "adjacent"): continue
        ppsf = L.get("price_per_sqft")
        if ppsf and ppsf > 0:
            by_nh.setdefault(L["neighborhood"], []).append(ppsf)
    medians = {nh: sorted(vals)[len(vals)//2] for nh, vals in by_nh.items() if len(vals) >= 3}
    for L in merged["listings"]:
        if L.get("status") not in ("active", "adjacent"):
            L["below_market_pct"] = None
            L["is_hidden_gem"] = False
            continue
        nh = L.get("neighborhood")
        ppsf = L.get("price_per_sqft")
        median = medians.get(nh)
        if median and ppsf:
            pct = (median - ppsf) / median * 100
            L["below_market_pct"] = round(pct, 1)  # positive = cheaper than median
        else:
            L["below_market_pct"] = None
        # Hidden-gem composite: ≥10% below median + (RC-likely OR pre-war language) + multi-unit
        rc = L.get("likely_rent_controlled_score") or 0
        is_below = (L.get("below_market_pct") or 0) >= 10
        is_old   = rc >= 5 or (L.get("year_built") or 9999) < 1979
        is_multi = (L.get("unit_count") or 0) >= 2
        L["is_hidden_gem"] = bool(is_below and (is_old or is_multi))

    merged["nh_medians_ppsf"] = medians

    new_count = sum(1 for L in merged["listings"] if L.get("is_new_since_last_refresh"))
    log_entry = {
        "timestamp": merged["last_refresh"],
        "duration_seconds": round(time.time() - started, 1),
        "sources": {
            "craigslist": {"pulled": len(cl), "errors": len(cl_err)},
            "zillow":     {"pulled": len(zw), "errors": len(zw_err)},
            "apartments": {"pulled": len(ap), "errors": len(ap_err)},
            "padmapper":  {"pulled": len(pm), "errors": len(pm_err)},
        },
        "totals": {
            "new": new_count,
            "updated": len(stats["fresh_ids"]) - new_count,
            "newly_inactive": stats["newly_inactive"],
            "active_total": sum(1 for L in merged["listings"] if L["status"] == "active"),
            "excluded_total": sum(1 for L in merged["listings"] if L["status"] == "excluded"),
            "inactive_total": sum(1 for L in merged["listings"] if L["status"] == "inactive"),
        },
        "errors": cl_err + zw_err + pm_err,
        "enriched_detail": enriched_detail,
        "enriched_llm": enriched_llm,
    }
    merged["refresh_log"] = [log_entry] + merged["refresh_log"]
    write_json(LISTINGS_PATH, merged)

    # Git push to GitHub (auto-deploys to Pages) — opt-in via --publish
    if getattr(args, "publish", False):
        try:
            subprocess.run(["git", "add", "data/listings.json", "data/pulls/"],
                           cwd=ROOT, check=True, capture_output=True, timeout=10)
            # Only commit if there are staged changes
            diff_check = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                        cwd=ROOT, capture_output=True, timeout=5)
            if diff_check.returncode != 0:  # there are changes
                msg = f"Refresh {merged['last_refresh']} (+{log_entry['totals']['new']} new)"
                subprocess.run(["git", "commit", "-m", msg],
                               cwd=ROOT, check=True, capture_output=True, timeout=10)
                subprocess.run(["git", "push"],
                               cwd=ROOT, check=True, capture_output=True, timeout=30)
                print("[refresh] published to GitHub")
            else:
                print("[refresh] nothing changed, skipping push")
        except subprocess.CalledProcessError as e:
            print(f"[refresh] git push failed: {e.stderr.decode() if e.stderr else e}", file=sys.stderr)
        except Exception as e:
            print(f"[refresh] publish error: {e}", file=sys.stderr)

    # macOS notification — only ping for genuinely new high-score listings
    if getattr(args, "notify", False):
        notable = [L for L in merged["listings"]
                   if L.get("is_new_since_last_refresh") and (L.get("score") or 0) >= 50]
        if notable:
            top = sorted(notable, key=lambda x: -(x.get("score") or 0))[:3]
            body = f"{len(notable)} new score≥50 listings. Top: " + " · ".join(
                f"{L['neighborhood']} ${L['price']} ({L['score']:.0f})" for L in top
            )
            macos_notify("SF Apt Search — new high-scoring listings", body)
        elif log_entry["totals"]["new"]:
            macos_notify("SF Apt Search refreshed",
                         f"+{log_entry['totals']['new']} new listings (none above score 50)")
    return log_entry


def serve(port: int, args) -> int:
    """HTTP server that serves the dashboard + exposes POST /api/refresh."""
    import http.server, socketserver, webbrowser

    class Handler(http.server.SimpleHTTPRequestHandler):
        # Serve relative to the project root, not cwd
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(ROOT), **kw)

        def log_message(self, fmt, *a):  # quieter
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", ""):
                self.send_response(302); self.send_header("Location", "/dashboard/"); self.end_headers(); return
            return super().do_GET()

        def do_POST(self):
            if self.path == "/api/refresh":
                try:
                    print(f"[serve] {now_iso()} — refresh requested via dashboard")
                    log_entry = run_refresh(args)
                    t = log_entry["totals"]
                    print(f"[serve] → +{t['new']} new, {t['newly_inactive']} newly inactive, {t['active_total']} active")
                    return self._json({"ok": True, "log_entry": log_entry})
                except Exception as e:
                    print(f"[serve] refresh failed: {e}", file=sys.stderr)
                    return self._json({"ok": False, "error": str(e)}, status=500)
            self._json({"error": "not found"}, status=404)

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
        url = f"http://127.0.0.1:{port}/dashboard/"
        print(f"[serve] Dashboard: {url}")
        print(f"[serve] Press Ctrl-C to stop.")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] stopped.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-cl", action="store_true", help="skip Craigslist pull")
    parser.add_argument("--no-zw", action="store_true", help="skip Zillow")
    parser.add_argument("--no-ap", action="store_true", help="skip Apartments.com")
    parser.add_argument("--pm",    action="store_true", help="include Padmapper (off by default — minimal value)")
    parser.add_argument("--max-pull-age-hours", type=float, default=24, help="max age of browser-pull cache files")
    parser.add_argument("--serve", nargs="?", const=8000, type=int, metavar="PORT",
                        help="run dashboard server with /api/refresh endpoint (default port 8000)")
    parser.add_argument("--no-detail", action="store_true",
                        help="skip the Zillow detail-page fetcher (saves ~30s per refresh)")
    parser.add_argument("--llm", action="store_true",
                        help="enrich with Anthropic LLM (needs ANTHROPIC_API_KEY env var)")
    parser.add_argument("--notify", action="store_true",
                        help="send a macOS notification when new high-scoring listings appear")
    parser.add_argument("--publish", action="store_true",
                        help="git add/commit/push after refresh — auto-deploys to GitHub Pages")
    args = parser.parse_args()

    if args.serve is not None:
        return serve(args.serve, args)

    print(f"[refresh] {now_iso()} — pulling from sources…")
    log_entry = run_refresh(args)
    t = log_entry["totals"]
    s = log_entry["sources"]
    print(f"  craigslist: {s['craigslist']['pulled']} listings ({s['craigslist']['errors']} errors)")
    print(f"  zillow:     {s['zillow']['pulled']} listings ({s['zillow']['errors']} errors)")
    print(f"  apartments: {s['apartments']['pulled']} listings ({s['apartments']['errors']} errors)")
    if args.pm: print(f"  padmapper:  {s['padmapper']['pulled']} listings ({s['padmapper']['errors']} errors)")
    print()
    print(f"[refresh] {t['new']} new, {t['newly_inactive']} newly inactive.")
    print(f"[refresh] → active {t['active_total']} · excluded {t['excluded_total']} · inactive {t['inactive_total']} "
          f"(in {log_entry['duration_seconds']}s)")
    if log_entry["errors"]:
        print(f"[refresh] {len(log_entry['errors'])} note(s):")
        for e in log_entry["errors"]:
            print(f"  - {e.get('source')}: {e.get('message')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
