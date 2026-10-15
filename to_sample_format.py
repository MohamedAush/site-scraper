#!/usr/bin/env python3
"""Rewrite {"docs": [...]} JSON files to the current sample.json shape.

- featuredImage: "url"  ->  {"url": "url", "alt": "<English title, or Dhivehi if none>"}
- files: ["url", ...]   ->  [{"label": "<file name>", "file": {"url": "url"}}, ...]

Labels come from the file name: URL-decoded, without the extension or the trailing upload
timestamp (e.g. ".../Iulaan%20150-1791778465021.pdf" -> "Iulaan 150"). Records that are
already in the new shape are left as they are.
"""

import argparse
import json
import os
import re
import sys
from urllib.parse import unquote, urlparse


def label_for(url):
    name = os.path.basename(unquote(urlparse(url).path))
    stem = os.path.splitext(name)[0]
    # Drop upload timestamps appended by the CMS, e.g. "-1791778465021" (one or more).
    stem = re.sub(r"(-\d{10,})+$", "", stem)
    stem = re.sub(r"[_\s]+", " ", stem).strip()
    return stem or name


def alt_for(doc):
    translations = doc.get("translations") or {}
    for locale in ("en", "dv"):
        title = (translations.get(locale) or {}).get("title")
        if title:
            return title
    return doc.get("title", "")


def convert(doc):
    out = dict(doc)
    image = doc.get("featuredImage")
    if isinstance(image, str) and image:
        out["featuredImage"] = {"url": image, "alt": alt_for(doc)}
    files = doc.get("files")
    if files:
        out["files"] = [
            entry if isinstance(entry, dict) else {"label": label_for(entry), "file": {"url": entry}}
            for entry in files
        ]
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="JSON files to convert")
    parser.add_argument("-o", "--output-dir", required=True, help="directory for the converted files")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    for path in args.inputs:
        with open(path, encoding="utf-8") as f:
            docs = [convert(doc) for doc in json.load(f)["docs"]]
        result = {"docs": docs, "totalDocs": len(docs), "hasNextPage": False, "nextPage": None}
        target = os.path.join(args.output_dir, os.path.basename(path))
        with open(target, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"{target}: {len(docs)} docs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
