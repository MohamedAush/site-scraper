#!/usr/bin/env python3
"""Write each record of a {"docs": [...]} JSON file as Markdown files, one per language.

Files go to <output-dir>/<locale>/<date>-<id>.md and start with a front matter block holding the
record's fields, followed by the title and body.
"""

import argparse
import json
import os
import sys


def front_matter(fields):
    lines = ["---"]
    for key, value in fields.items():
        if value:
            # JSON strings are valid YAML, which keeps quotes and colons in titles safe.
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def render(doc, locale, fields):
    meta = {
        "id": doc["id"],
        "locale": locale,
        "title": fields["title"],
        "description": fields.get("description"),
        "source_type": doc.get("source_type"),
        "published_at": doc.get("published_at"),
        "featuredImage": doc.get("featuredImage"),
    }
    parts = [front_matter(meta), f"# {fields['title']}"]
    if doc.get("featuredImage"):
        parts.append(f"![]({doc['featuredImage']})")
    if fields.get("body"):
        parts.append(fields["body"])
    for entry in doc.get("files") or []:
        if isinstance(entry, dict):
            parts.append(f"[{entry['label']}]({entry['file']['url']})")
        else:
            parts.append(f"<{entry}>")
    return "\n\n".join(parts) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="JSON file to convert")
    parser.add_argument("-o", "--output-dir", required=True, help="directory for the Markdown files")
    args = parser.parse_args()

    with open(args.input, encoding="utf-8") as f:
        docs = json.load(f)["docs"]

    counts = {}
    for doc in docs:
        # Records are either bilingual ("translations") or a single language ("locale").
        versions = doc.get("translations") or {doc.get("locale", "und"): doc}
        for locale, fields in versions.items():
            folder = os.path.join(args.output_dir, locale)
            os.makedirs(folder, exist_ok=True)
            name = f"{(doc.get('published_at') or '')[:10]}-{doc['id']}.md".lstrip("-")
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write(render(doc, locale, fields))
            counts[locale] = counts.get(locale, 0) + 1

    for locale, count in counts.items():
        print(f"{count} files -> {os.path.join(args.output_dir, locale)}/", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
