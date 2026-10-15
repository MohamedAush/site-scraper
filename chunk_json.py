#!/usr/bin/env python3
"""Merge one or more {"docs": [...]} JSON files and split the result into numbered chunk files.

Each chunk keeps the same structure, with totalDocs set to the overall total and hasNextPage /
nextPage pointing at the following chunk.
"""

import argparse
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", help="JSON files to merge, in order")
    parser.add_argument("-o", "--output-dir", required=True, help="directory for the chunk files")
    parser.add_argument("-n", "--size", type=int, default=100, help="docs per chunk (default: 100)")
    parser.add_argument("--name", default="chunk", help="file name prefix (default: chunk)")
    args = parser.parse_args()

    docs = []
    for path in args.inputs:
        with open(path, encoding="utf-8") as f:
            docs.extend(json.load(f)["docs"])

    ids = [doc["id"] for doc in docs]
    if len(set(ids)) != len(ids):
        print("error: the input files share some ids", file=sys.stderr)
        return 1
    # Newest first; the sort is stable, so docs with the same date keep their input order.
    docs.sort(key=lambda doc: doc.get("published_at") or "", reverse=True)

    os.makedirs(args.output_dir, exist_ok=True)
    pages = max(1, -(-len(docs) // args.size))
    for page in range(1, pages + 1):
        chunk = docs[(page - 1) * args.size:page * args.size]
        result = {
            "docs": chunk,
            "totalDocs": len(docs),
            "hasNextPage": page < pages,
            "nextPage": page + 1 if page < pages else None,
        }
        path = os.path.join(args.output_dir, f"{args.name}-{page}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"{path}: {len(chunk)} docs", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
