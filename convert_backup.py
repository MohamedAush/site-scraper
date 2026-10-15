#!/usr/bin/env python3
"""Convert the posts in a MySQL dump of the mach.gov.mv CMS into JSON files.

Each post type (news, galleries, jobs, ...) is written to its own file, shaped like
{"docs": [...], "totalDocs": N, "hasNextPage": false, "nextPage": null}, with Dhivehi and English
held under "translations" and bodies converted to Markdown. Only published, undeleted posts are
included unless --all is given.
"""

import argparse
import html
import json
import os
import re
import sys
from urllib.parse import quote, urljoin

from markdownify import markdownify

SITE_URL = "https://mach.gov.mv"
# Post type -> id prefix. source_type comes from the post type's name in the dump.
ID_PREFIXES = {
    "news": "article", "gaumiyyath": "gaumiyyath", "occasions": "occasion", "tharika": "tharika",
    "downloads": "download", "announcements": "announcement", "publications": "publication", "jobs": "job",
    "galleries": "gallery", "tenders": "tender", "releases": "release", "pages": "page", "idharaa": "idharaa",
}
# Labels for details that have no field of their own in the output and are added to the body.
LABELS = {
    "en": {"ref_no": "Reference no.", "document_no": "Document no.", "deadline": "Deadline", "video": "Video"},
    "dv": {"ref_no": "ރެފަރެންސް ނަންބަރު", "document_no": "ޑޮކިއުމަންޓް ނަންބަރު", "deadline": "ސުންގަޑި",
           "video": "ވީޑިއޯ"},
}
SQL_ESCAPES = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "Z": "\x1a"}
THAANA = re.compile("[ހ-޿]")


def parse_values(text, i):
    """Yield the rows of a mysqldump 'INSERT ... VALUES (..),(..);' statement, starting at index i."""
    while i < len(text):
        while text[i] in " ,\n":
            i += 1
        if text[i] == ";":
            return
        i += 1  # opening bracket
        row = []
        while True:
            if text[i] == "'":
                i += 1
                parts = []
                while True:
                    j = i
                    while text[j] not in "'\\":
                        j += 1
                    parts.append(text[i:j])
                    i = j
                    if text[i] == "\\":
                        parts.append(SQL_ESCAPES.get(text[i + 1], text[i + 1]))
                        i += 2
                    elif text[i + 1] == "'":
                        parts.append("'")
                        i += 2
                    else:
                        i += 1
                        break
                row.append("".join(parts))
            else:
                j = i
                while text[j] not in ",)":
                    j += 1
                row.append(None if text[i:j] == "NULL" else text[i:j])
                i = j
            if text[i] == ")":
                i += 1
                break
            i += 1  # comma between values
        yield row


def load_tables(path, wanted):
    """Read the named tables from the dump as lists of {column: value} rows."""
    tables = {name: [] for name in wanted}
    columns = {}
    current = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            match = re.match(r"CREATE TABLE `([^`]+)`", line)
            if match:
                current = match.group(1)
                columns[current] = []
            elif current and line.startswith("  `"):
                columns[current].append(line.split("`")[1])
            else:
                match = re.match(r"INSERT INTO `([^`]+)` VALUES ", line)
                if match and match.group(1) in tables:
                    name = match.group(1)
                    tables[name].extend(dict(zip(columns[name], row)) for row in parse_values(line, match.end()))
    return tables


def decode_translations(value):
    """Unwrap the CMS's translations column.

    It is JSON that was re-encoded on every save, with the previous value nested under "0".
    Layers are merged so that the outermost (newest) value of each field wins.
    """
    while isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    if not isinstance(value, dict):
        return {}
    merged = decode_translations(value["0"]) if "0" in value else {}
    merged.update({key: item for key, item in value.items() if key != "0"})
    return merged


def inline(text):
    """Editor text with its inline HTML (bold, links, line breaks) as Markdown on one line."""
    text = text or ""
    if "<" in text:
        text = markdownify(text, escape_asterisks=False, escape_underscores=False)
    else:
        text = html.unescape(text)
    return " ".join(text.split())


def absolute(url):
    return urljoin(SITE_URL, url or "")


def media_url(media):
    return f"{SITE_URL}/storage/uploads/{media['uuid']}/{quote(media['file_name'])}"


def blocks_to_markdown(content, media, files):
    """Convert Editor.js content to Markdown. Documents embedded in the content are added to files."""
    try:
        blocks = json.loads(content or "{}").get("blocks", [])
    except ValueError:
        return ""
    out = []
    for block in blocks:
        kind, data = block.get("type"), block.get("data", {})
        if kind == "paragraph":
            out.append(inline(data.get("text")))
        elif kind == "header":
            out.append("#" * int(data.get("level", 2)) + " " + inline(data.get("text")))
        elif kind == "list":
            ordered = data.get("style") == "ordered"
            out.append("\n".join(f"{f'{n}.' if ordered else '-'} {inline(item)}"
                                 for n, item in enumerate(data.get("items", []), 1)))
        elif kind == "table":
            rows = [[inline(cell).replace("|", "\\|") for cell in row] for row in data.get("content", [])]
            if rows:
                lines = ["| " + " | ".join(row) + " |" for row in rows]
                lines.insert(1, "|" + " --- |" * len(rows[0]))
                out.append("\n".join(lines))
        elif kind == "media_image":
            out.append(f"![{inline(data.get('title'))}]({absolute(data.get('url'))})")
        elif kind == "document":
            # The block's own url may be a preview image, so the media record is used when known.
            item = media.get(str(data.get("media")))
            url = media_url(item) if item else absolute(data.get("url"))
            label = inline(data.get("title")) or (item or {}).get("name") or os.path.basename(url)
            add_file(files, label, url)
    return "\n\n".join(part for part in out if part)


def add_file(files, label, url):
    if url not in [entry["file"]["url"] for entry in files]:
        files.append({"label": label, "file": {"url": url}})


def extras(post, locale, gallery):
    """Markdown for reference numbers, deadlines, videos and gallery images, which have no output field."""
    labels = LABELS[locale]
    lines = []
    if post.get("ref_no"):
        lines.append(f"**{labels['ref_no']}:** {post['ref_no']}")
    if post.get("document_no"):
        lines.append(f"**{labels['document_no']}:** {post['document_no']}")
    if post.get("expire_at"):
        lines.append(f"**{labels['deadline']}:** {post['expire_at'][:16]}")
    if post.get("video_url"):
        lines.append(f"**{labels['video']}:** {post['video_url']}")
    lines.extend(f"![]({url})" for url in gallery)
    return "\n\n".join(lines)


def translation(title, description, body):
    fields = {"title": " ".join(title.split())}
    if description and description.strip():
        fields["description"] = " ".join(description.split())
    fields["body"] = body
    return fields


def build_doc(post, source_type, media, attachments):
    attached = attachments.get(post["id"], {})
    files = []
    gallery = [media_url(item) for item in attached.get("image_gallery", [])]

    def body(content, locale):
        parts = [blocks_to_markdown(content, media, files), extras(post, locale, gallery)]
        return "\n\n".join(part for part in parts if part)

    doc = {
        "id": f"{ID_PREFIXES.get(post['type'], post['type'])}-{post['id']}",
        "source_type": source_type,
        # Stored without a timezone; most are midnight, i.e. a date picked in the CMS.
        "published_at": (post["published_at"] or post["created_at"]).replace(" ", "T") + "Z",
        "translations": {},
    }
    # The translations column is meant to hold English, but for many posts editors pasted the
    # Dhivehi text there again; those are not treated as an English version.
    english = decode_translations(post.get("translations"))
    if english.get("title") and not THAANA.search(english["title"]):
        doc["translations"]["en"] = translation(english["title"], english.get("excerpt"),
                                                body(english.get("content"), "en"))
    doc["translations"]["dv"] = translation(post["title"], post.get("excerpt"), body(post["content"], "dv"))

    featured = attached.get("featured_image")
    if featured:
        doc["featuredImage"] = media_url(featured[0])
    for item in attached.get("documents", []):
        add_file(files, item["name"], media_url(item))
    if files:
        doc["files"] = files
    return doc


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dump", help="path to the .sql dump")
    parser.add_argument("-o", "--output-dir", default="mach", help="directory for the JSON files (default: mach)")
    parser.add_argument("--all", action="store_true", help="also include drafts, pending and deleted posts")
    args = parser.parse_args()

    tables = load_tables(args.dump, ["posts", "post_types", "media", "attachments"])
    if not tables["posts"]:
        print("error: no posts found in the dump", file=sys.stderr)
        return 1

    media = {item["id"]: item for item in tables["media"]}
    # post id -> collection (featured_image, image_gallery, documents) -> media records, in display order
    attachments = {}
    for row in sorted(tables["attachments"], key=lambda r: (int(r["order_column"] or 0), int(r["id"]))):
        if row["model_type"] == "post" and row["media_id"] in media:
            attachments.setdefault(row["model_id"], {}).setdefault(row["collection_name"], []).append(
                media[row["media_id"]])

    type_names = {row["slug"]: row["name"] for row in tables["post_types"]}
    by_type = {slug: [] for slug in type_names}
    skipped = 0
    for post in tables["posts"]:
        if not args.all and (post["deleted_at"] or post["status"] != "published"):
            skipped += 1
            continue
        by_type.setdefault(post["type"], []).append(
            build_doc(post, type_names.get(post["type"], post["type"]), media, attachments))

    os.makedirs(args.output_dir, exist_ok=True)
    total = 0
    for slug, docs in by_type.items():
        if not docs:
            continue
        docs.sort(key=lambda doc: doc["published_at"], reverse=True)
        path = os.path.join(args.output_dir, slug + ".json")
        result = {"docs": docs, "totalDocs": len(docs), "hasNextPage": False, "nextPage": None}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"{slug}: {len(docs)} docs -> {path}", file=sys.stderr)
        total += len(docs)

    print(f"Saved {total} docs under {args.output_dir}/ ({skipped} unpublished or deleted posts left out)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
