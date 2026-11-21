"""Add lat/lon to each company from the city table (companies/cities.csv).

City-level precision is enough for a radius in km. No network calls.
Companies whose city starts with "Remote" (e.g. "Remote DE") get no
coordinates and are kept as remote, with country taken from the suffix.
Home coordinates are supplied by the private caller, never stored here.

Usage:
  python -m pendel.locate companies/companies-ats.csv companies/companies-located.csv
"""
import argparse
import csv
import math
import sys
import unicodedata
from pathlib import Path

CITIES = Path(__file__).resolve().parents[2] / "companies" / "cities.csv"
def norm(name):
    s = unicodedata.normalize("NFKD", name.strip().lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def load_cities(path=CITIES):
    with open(path, newline="", encoding="utf-8") as f:
        return {norm(r["city"]): (float(r["lat"]), float(r["lon"]), r["country"]) for r in csv.DictReader(f)}


def km(a, b):
    t = math.pi / 180
    dlat, dlon = (b[0] - a[0]) * t, (b[1] - a[1]) * t
    h = math.sin(dlat / 2) ** 2 + math.cos(a[0] * t) * math.cos(b[0] * t) * math.sin(dlon / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def locate(rows, cities, home=None):
    """Return (rows with lat/lon/country/km_from_home added, sorted list of unknown cities)."""
    missing = set()
    out = []
    for row in rows:
        row = dict(row)
        city = row.get("city", "").strip()
        if norm(city).startswith("remote"):
            suffix = city.split()[-1].upper() if len(city.split()) > 1 else ""
            row.update(lat="", lon="", country=suffix if suffix in ("DE", "NL", "EU") else "", km_from_home="")
            out.append(row)
            continue
        hit = cities.get(norm(city))
        if hit is None:
            missing.add(row.get("city", ""))
            row.update(lat="", lon="", country="", km_from_home="")
        else:
            lat, lon, country = hit
            row.update(lat=f"{lat:.4f}", lon=f"{lon:.4f}", country=country,
                       km_from_home=str(round(km(home, (lat, lon)))) if home else "")
        out.append(row)
    return out, sorted(missing)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output", help="new output csv (must not exist)")
    ap.add_argument("--home-lat", type=float, help="optional private home latitude")
    ap.add_argument("--home-lon", type=float, help="optional private home longitude")
    args = ap.parse_args()

    with open(args.input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if "city" not in fieldnames:
        sys.exit(f"error: no 'city' column. Header is: {fieldnames}")

    if (args.home_lat is None) != (args.home_lon is None):
        sys.exit("error: pass both --home-lat and --home-lon, or neither")
    home = (args.home_lat, args.home_lon) if args.home_lat is not None else None
    located, missing = locate(rows, load_cities(), home)
    if missing:
        sys.exit("error: cities not in companies/cities.csv: " + ", ".join(missing))

    fields = fieldnames + [c for c in ["lat", "lon", "country", "km_from_home"] if c not in fieldnames]
    try:
        out = open(args.output, "x", newline="", encoding="utf-8")
    except FileExistsError:
        sys.exit(f"error: {args.output} already exists. Pick a new output file name.")
    with out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(located)
    print(f"located {len(located)} companies -> {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
