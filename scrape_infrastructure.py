#!/usr/bin/env python3
"""Download the public registries from infrastructure.gov.mv and save each one as a CSV file."""

import argparse
import csv
import os
import sys
import time

import requests

API_URL = "https://infrastructure.gov.mv/api/registries/entries"
USER_AGENT = "simple-crawler/1.0"
REGISTRIES = [
    "consultants",
    "dangerous-buildings",
    "architectural-checkers",
    "structural-checkers",
    "building-inspectors",
    "consent-authorities",
    "construction-associations",
    "testing-labs",
]
# Columns every registry has; the fields specific to a registry follow them.
COMMON_COLUMNS = ["name", "registration_number", "status", "expiry_date"]
PAGE_SIZE = 200  # the API caps the limit at 200 whatever is asked for


def fetch_entries(session, slug, delay, timeout):
    """Return every entry of one registry, following pagination."""
    entries = []
    page = 1
    while True:
        resp = session.get(API_URL, params={"slug": slug, "page": page, "limit": PAGE_SIZE}, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        entries.extend(payload["data"])
        if page >= payload["meta"]["totalPages"]:
            break
        page += 1
        time.sleep(delay)
    if len(entries) != payload["meta"]["total"]:
        print(f"  warning: {slug}: got {len(entries)} of {payload['meta']['total']}", file=sys.stderr)
    return entries


def cell(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return " ".join(str(value).split())


def to_rows(entries):
    """Flatten entries into a header and rows, with each registry's extra fields as columns."""
    extra_columns = []
    for entry in entries:
        for key in entry.get("extra_fields") or {}:
            if key not in extra_columns:
                extra_columns.append(key)

    rows = []
    for entry in entries:
        extra = entry.get("extra_fields") or {}
        row = [cell(entry.get(column)) for column in COMMON_COLUMNS]
        # Dates arrive as midnight timestamps; keep the day only.
        row[COMMON_COLUMNS.index("expiry_date")] = row[COMMON_COLUMNS.index("expiry_date")][:10]
        rows.append(row + [cell(extra.get(column)) for column in extra_columns])
    return COMMON_COLUMNS + extra_columns, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output-dir", default="infrastructure",
                        help="directory for the CSV files (default: infrastructure)")
    parser.add_argument("-r", "--registries", nargs="+", default=REGISTRIES, metavar="SLUG",
                        help="registries to download (default: all): " + ", ".join(REGISTRIES))
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    os.makedirs(args.output_dir, exist_ok=True)

    failed = []
    for index, slug in enumerate(args.registries):
        if index:
            time.sleep(args.delay)
        try:
            entries = fetch_entries(session, slug, args.delay, args.timeout)
        except (requests.RequestException, KeyError, ValueError) as exc:
            print(f"error: {slug}: {exc}", file=sys.stderr)
            failed.append(slug)
            continue

        header, rows = to_rows(entries)
        path = os.path.join(args.output_dir, slug + ".csv")
        # utf-8-sig so Excel reads non-English characters correctly.
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        print(f"{slug}: {len(rows)} rows -> {path}", file=sys.stderr)

    if failed:
        print(f"Could not download: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
