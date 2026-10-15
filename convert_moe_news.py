#!/usr/bin/env python3
"""Convert the Ministry of Education news export to the sample JSON structure.

The export is already close: this flattens featuredImage to a URL, moves the extra "images" list
into the article body as Markdown images, drops the "status" field, and makes sure every
translation has a body. Published articles and drafts are written to separate files.
"""

import argparse
import json
import os
import sys

import requests

SOURCE_URL = "https://www.moe.gov.mv/storage/migration_data/news/news.json"
USER_AGENT = "simple-crawler/1.0"


def load(source, timeout):
    if source.startswith(("http://", "https://")):
        resp = requests.get(source, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def convert(doc):
    featured = doc.get("featuredImage")
    featured_url = featured.get("url") if isinstance(featured, dict) else featured
    images = [url for url in doc.get("images") or [] if url != featured_url]

    out = {
        "id": doc["id"],
        "source_type": doc["source_type"],
        "published_at": doc["published_at"],
        "translations": {},
    }
    for locale, fields in doc["translations"].items():
        body = (fields.get("body") or "").strip()
        # The sample has no gallery field, so photos not already in the body are appended to it.
        gallery = "\n\n".join(f"![]({url})" for url in images if url not in body)
        translation = {"title": fields["title"].strip()}
        if (fields.get("description") or "").strip():
            translation["description"] = fields["description"].strip()
        translation["body"] = "\n\n".join(part for part in (body, gallery) if part)
        out["translations"][locale] = translation

    if featured_url:
        out["featuredImage"] = featured_url
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", nargs="?", default=SOURCE_URL, help=f"URL or file to convert (default: {SOURCE_URL})")
    parser.add_argument("-o", "--output", default="moe/news.json", help="output file (default: moe/news.json)")
    parser.add_argument("--drafts-output", default="moe/news-drafts.json",
                        help="output file for records that are not published (default: moe/news-drafts.json)")
    parser.add_argument("--timeout", type=float, default=120.0, help="download timeout in seconds (default: 120)")
    args = parser.parse_args()

    try:
        source = load(args.source, args.timeout)
    except (requests.RequestException, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    published = [doc for doc in source["docs"] if doc.get("status", "published") == "published"]
    drafts = [doc for doc in source["docs"] if doc.get("status", "published") != "published"]
    for path, group in ((args.output, published), (args.drafts_output, drafts)):
        docs = [convert(doc) for doc in group]
        result = {"docs": docs, "totalDocs": len(docs), "hasNextPage": False, "nextPage": None}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Saved {len(docs)} docs to {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
