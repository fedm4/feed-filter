#!/usr/bin/env python
"""Fetch every catalogued feed and check it is a parseable feed with entries in it.

A URL in the catalogue that has never been fetched is a guess, and guesses in a source
list surface much later as a feed that silently produces nothing. This is what stands
between the two.

    uv run scripts/verify_catalog.py               # your catalogue
    uv run scripts/verify_catalog.py --example     # the one that ships
    uv run scripts/verify_catalog.py --all         # disabled entries too

Exits non-zero if any enabled feed fails, so it can gate a commit.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from xml.etree import ElementTree

import httpx

from feedfilter.catalog import EXAMPLE_PATH, CatalogEntry, CatalogError, load_catalog

# Deliberately not feedparser. The question here is only "is this XML, and does it hold
# entries", which the standard library answers; the fetcher in D3 is where a real parser
# earns its place.
ENTRY_TAGS = {
    "item",  # RSS 2.0 and RDF
    "{http://www.w3.org/2005/Atom}entry",  # Atom
}

# Some feeds answer 403 to a bare client. Announcing what we are is both politer and more
# likely to work than pretending to be a browser.
HEADERS = {"user-agent": "feed-filter/0.1 (+https://github.com/fedm4/feed-filter)"}


@dataclass
class Result:
    entry: CatalogEntry
    ok: bool
    detail: str
    entries: int = 0


def count_entries(body: bytes) -> int:
    root = ElementTree.fromstring(body)
    return sum(1 for element in root.iter() if element.tag in ENTRY_TAGS)


def check(client: httpx.Client, entry: CatalogEntry) -> Result:
    try:
        response = client.get(entry.url)
    except httpx.HTTPError as exc:
        return Result(entry, False, f"unreachable: {type(exc).__name__}")

    if response.status_code != 200:
        return Result(entry, False, f"HTTP {response.status_code}")

    try:
        found = count_entries(response.content)
    except ElementTree.ParseError as exc:
        return Result(entry, False, f"not XML: {exc}")

    if found == 0:
        return Result(entry, False, "parsed, but no entries")
    return Result(entry, True, f"{found} entries", entries=found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="check disabled entries too")
    parser.add_argument(
        "--example",
        action="store_true",
        help="check catalog.example.yaml instead of your own catalogue",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    try:
        entries = load_catalog(EXAMPLE_PATH if args.example else None)
    except CatalogError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not args.all:
        entries = [entry for entry in entries if entry.enabled]

    results: list[Result] = []
    with httpx.Client(timeout=args.timeout, follow_redirects=True, headers=HEADERS) as client:
        for entry in entries:
            result = check(client, entry)
            results.append(result)
            mark = "ok  " if result.ok else "FAIL"
            flag = "" if result.entry.enabled else "  (disabled)"
            print(f"{mark}  {result.entry.name:<22} {result.detail}{flag}", flush=True)

    failed = [r for r in results if not r.ok and r.entry.enabled]
    print(f"\n{len(results) - len(failed)}/{len(results)} ok")
    if failed:
        print("\nFailing, and enabled:")
        for result in failed:
            print(f"  {result.entry.name}: {result.detail}\n    {result.entry.url}")
        print("\nEither fix the URL or ship it as `enabled: false` with a comment saying why.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
