#!/usr/bin/env python3
"""Scrape articles from fishagriocean.gov.mv (Ministry of Fisheries, Agriculture and Ocean Resources).

The site is a Next.js app that embeds each page's data as JSON, with the Dhivehi and English
versions of an item held together. Every section under Media and Publications is written to its
own JSON file, shaped like {"docs": [...], "totalDocs": N, "hasNextPage": false, "nextPage": null},
with one bilingual record per item and bodies converted to Markdown.
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import quote, urlparse

import requests

BASE_URL = "https://fishagriocean.gov.mv"
ASSET_URL = "https://fisheries.gov.mv/images"
FEATURED_IMAGE_WIDTH = 1500
USER_AGENT = "simple-crawler/1.0"

# Section (also its output path) -> list page, detail page, id prefix and the source_type to emit.
SECTIONS = {
    "media/news": {"list": "/media/news", "detail": "/media/news", "id_prefix": "article", "source_type": "News"},
    "media/events": {"list": "/media/events", "detail": "/media/events", "id_prefix": "event", "source_type": "Event"},
    "media/press-release": {"list": "/media/press-release", "detail": "/media/press-release",
                            "id_prefix": "press-release", "source_type": "Press Release"},
    "media/speeches": {"list": "/media/speeches", "detail": "/media/speeches",
                       "id_prefix": "speech", "source_type": "Speech"},
    "media/circulars": {"list": "/media/circulars", "detail": "/media/circulars",
                        "id_prefix": "circular", "source_type": "Circular"},
    "media/announcements": {"list": "/media/announcements", "detail": "/media/announcements",
                            "id_prefix": "announcement", "source_type": "Announcement"},
    # Photo and video galleries share one id sequence and one detail page.
    "media/photo-gallery": {"list": "/media/gallery/photo-gallery", "detail": "/media/gallery",
                            "id_prefix": "gallery", "source_type": "Photo Gallery"},
    "media/video-gallery": {"list": "/media/gallery/video-gallery", "detail": "/media/gallery",
                            "id_prefix": "gallery", "source_type": "Video Gallery"},
    "publications/guidelines": {"list": "/publications/guidelines", "detail": "/publications/guidelines",
                                "id_prefix": "guideline", "source_type": "Guideline"},
    "publications/statistics": {"list": "/publications/statistics", "detail": "/publications/statistics",
                                "id_prefix": "statistics", "source_type": "Statistics"},
    "publications/market": {"list": "/publications/market", "detail": "/publications/market",
                            "id_prefix": "market", "source_type": "Market"},
    "publications/resources": {"list": "/publications/resources", "detail": "/publications/resources",
                               "id_prefix": "resource", "source_type": "Resource"},
    "publications/laws-regulations": {"list": "/publications/laws-regulations",
                                      "detail": "/publications/laws-regulations",
                                      "id_prefix": "law-regulation", "source_type": "Laws and Regulations"},
}
# Labels for details that have no field of their own in the output and are added to the body.
LABELS = {
    "en": {"date": "Date", "time": "Time", "location": "Location", "deadline": "Deadline", "video": "Video"},
    "dv": {"date": "ތާރީޚު", "time": "ގަޑި", "location": "ތަން", "deadline": "ސުންގަޑި", "video": "ވީޑިއޯ"},
}
FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def get_page_props(session, url, params, timeout, retries=3):
    """Fetch a page and return the data Next.js embedded in it."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code < 500:
                resp.raise_for_status()
                match = NEXT_DATA.search(resp.text)
                if not match:
                    raise RuntimeError(f"{url}: no embedded page data found")
                return json.loads(match.group(1))["props"]["pageProps"]
            error = f"HTTP {resp.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            error = str(exc)
        if attempt == retries:
            raise RuntimeError(f"{url} failed after {retries} attempts: {error}")
        time.sleep(2 * attempt)


def list_ids(session, base_url, section, limit, delay, timeout):
    """Walk a section's list pages and return its article ids, newest first."""
    url = base_url + SECTIONS[section]["list"]
    ids = []
    page = 1
    while True:
        props = get_page_props(session, url, {"page": page}, timeout)
        items = [item for key, value in props.items() if key != "cats" and isinstance(value, list)
                 for item in value if isinstance(item, dict) and "sequenceId" in item]
        new = [item["sequenceId"] for item in items if item["sequenceId"] not in ids]
        if not new:
            break
        ids.extend(new)
        if limit and len(ids) >= limit:
            break
        page += 1
        time.sleep(delay)
    ids.sort(reverse=True)
    return ids[:limit] if limit else ids


def apply_marks(text, marks):
    """Wrap text in Markdown for its marks, keeping surrounding whitespace outside the markers."""
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[:len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    for mark in marks:
        kind = mark.get("type")
        if kind == "bold":
            stripped = f"**{stripped}**"
        elif kind == "italic":
            stripped = f"*{stripped}*"
        elif kind == "strike":
            stripped = f"~~{stripped}~~"
        elif kind == "code":
            stripped = f"`{stripped}`"
        elif kind == "link" and mark.get("attrs", {}).get("href"):
            stripped = f"[{stripped}]({mark['attrs']['href']})"
    return lead + stripped + trail


def inline(nodes):
    parts = []
    for node in nodes or []:
        kind = node.get("type")
        if kind == "text":
            parts.append(apply_marks(node.get("text", ""), node.get("marks", [])))
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "image":
            parts.append(image(node))
        else:
            parts.append(inline(node.get("content")))
    return "".join(parts)


def image(node):
    attrs = node.get("attrs", {})
    return f"![{attrs.get('alt') or ''}]({attrs.get('src', '')})"


def list_items(node, ordered):
    lines = []
    for number, item in enumerate(node.get("content") or [], 1):
        marker = f"{number}." if ordered else "-"
        text = "\n\n".join(blocks(item.get("content")))
        indented = text.replace("\n", "\n" + " " * (len(marker) + 1))
        lines.append(f"{marker} {indented}")
    return "\n".join(lines)


def blocks(nodes):
    """Convert block-level editor nodes to a list of Markdown blocks."""
    out = []
    for node in nodes or []:
        kind = node.get("type")
        if kind == "doc":
            out.extend(blocks(node.get("content")))
        elif kind == "heading":
            out.append("#" * node.get("attrs", {}).get("level", 2) + " " + inline(node.get("content")).strip())
        elif kind == "bulletList":
            out.append(list_items(node, ordered=False))
        elif kind == "orderedList":
            out.append(list_items(node, ordered=True))
        elif kind == "blockquote":
            out.append("\n".join("> " + line for line in "\n\n".join(blocks(node.get("content"))).split("\n")))
        elif kind == "codeBlock":
            out.append("```\n" + inline(node.get("content")) + "\n```")
        elif kind == "horizontalRule":
            out.append("---")
        elif kind == "image":
            out.append(image(node))
        elif kind in ("table", "tableRow"):
            out.extend(blocks(node.get("content")))
        elif kind in ("tableCell", "tableHeader"):
            out.append(" ".join(blocks(node.get("content"))))
        else:
            out.append(inline(node.get("content")).strip())
    return [block for block in out if block.strip()]


def to_markdown(body):
    text = "\n\n".join(blocks(body)).replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def clean(text):
    return " ".join((text or "").split())


def day(value):
    return (value or "")[:10]


def extras(item, locale):
    """Markdown for event details, deadlines, gallery photos and videos, which the output has no fields for."""
    labels = LABELS[locale]
    suffix = "En" if locale == "en" else ""
    lines = []

    start, end = day(item.get("startDate") or item.get("date")), day(item.get("endDate"))
    if start:
        lines.append(f"**{labels['date']}:** {start}" + (f" – {end}" if end and end != start else ""))
    if clean(item.get("time" + suffix)):
        lines.append(f"**{labels['time']}:** {clean(item['time' + suffix])}")
    if clean(item.get("location" + suffix)):
        lines.append(f"**{labels['location']}:** {clean(item['location' + suffix])}")
    if item.get("deadline"):
        lines.append(f"**{labels['deadline']}:** {day(item['deadline'])}")
    if clean(item.get("youTubeLink")):
        lines.append(f"**{labels['video']}:** {clean(item['youTubeLink'])}")
    lines.extend(f"![]({image_url(name)})" for name in (item.get("photos") or []) + (item.get("images") or []))
    return "\n\n".join(lines)


def image_url(name):
    return f"{ASSET_URL}/{FEATURED_IMAGE_WIDTH}/{quote(name)}.webp"


def translation(item, locale):
    suffix = "En" if locale == "en" else ""
    fields = {"title": clean(item.get("title" + suffix))}
    description = clean(item.get("description" + suffix))
    if description and description != "-":
        fields["description"] = description
    body = [to_markdown(item.get("body" + suffix)), extras(item, locale)]
    fields["body"] = "\n\n".join(part for part in body if part)
    return fields


def file_list(files, label_mode):
    """Attached files as plain URLs, or as {"label", "file": {"url"}} objects when label_mode is set.

    label_mode picks the label text: "en", "dv", or "both" for "English / Dhivehi".
    """
    urls = []
    out = []
    for entry in files or []:
        if entry.get("file"):
            url = f"{ASSET_URL}/{quote(entry['file'])}"
        elif urlparse(entry.get("href") or "").path.lower().endswith(FILE_EXTENSIONS):
            url = entry["href"]
        else:
            continue
        if url in urls:
            continue
        urls.append(url)
        if label_mode:
            titles = {"en": clean(entry.get("titleEn")), "dv": clean(entry.get("title"))}
            if label_mode == "both":
                names = [titles["en"]] + [titles["dv"]] * (titles["dv"] != titles["en"])
            else:
                # Fall back to the other language when the chosen one is missing.
                names = [titles[label_mode] or titles["en"] or titles["dv"]]
            label = " / ".join(name for name in names if name) or os.path.basename(urlparse(url).path)
            out.append({"label": label, "file": {"url": url}})
        else:
            out.append(url)
    return out


def build_docs(article, section, file_labels):
    """Build the output records for one item.

    Normally that is one record holding both languages under "translations", with files beside
    the id so they apply to both. Publications with file_labels "split" give one record per
    language instead, linked by "translation_group", so each language carries its own file labels.
    """
    group = f"{SECTIONS[section]['id_prefix']}-{article['sequenceId']}"
    common = {
        "source_type": SECTIONS[section]["source_type"],
        "published_at": re.sub(r"\.\d+Z$", "Z", article.get("publishedDate") or article.get("createdAt") or ""),
    }
    # Unsuffixed fields are Dhivehi; the English ones carry an "En" suffix.
    locales = [locale for locale, key in (("en", "titleEn"), ("dv", "title")) if clean(article.get(key))]
    labelled = section.startswith("publications/")

    if labelled and file_labels == "split":
        docs = [dict({"id": f"{group}-{locale}", "translation_group": group, "locale": locale}, **common,
                     **translation(article, locale)) for locale in locales]
    else:
        docs = [dict({"id": group}, **common,
                     translations={locale: translation(article, locale) for locale in locales})]

    for doc in docs:
        if article.get("thumbnail"):
            doc["featuredImage"] = image_url(article["thumbnail"])
        label_mode = (doc.get("locale") or file_labels) if labelled else None
        files = file_list(article.get("files"), label_mode)
        if files:
            doc["files"] = files
    return docs


def fetch_article(session, base_url, section, sequence_id, timeout, retries=3):
    url = f"{base_url}{SECTIONS[section]['detail']}/{sequence_id}"
    props = get_page_props(session, url, None, timeout, retries)
    return next((v for v in props.values() if isinstance(v, dict) and "sequenceId" in v), None)


def fetch_skipped(session, base_url, section, ids, args):
    """Fetch items the list pages left out.

    The site's list pages sort by date only, so items sharing a date can repeat on one page and
    never appear on the next. Ids are sequential, so every gap is tried directly; an id that was
    deleted or belongs to another section answers with an error and is ignored.
    """
    found = []
    for sequence_id in sorted(set(range(1, max(ids))) - set(ids), reverse=True):
        time.sleep(args.delay)
        try:
            article = fetch_article(session, base_url, section, sequence_id, args.timeout, retries=1)
        except (RuntimeError, requests.RequestException):
            continue
        if not article or article.get("status", "published") != "published":
            continue
        # Photo and video galleries share ids, so a gap in one is usually an item of the other.
        if "video" in article and article["video"] != (section == "media/video-gallery"):
            continue
        found.append(article)
    return found


def scrape_section(session, base_url, section, args):
    """Scrape one section. Returns its docs, the ids that failed, and whether the run was cut short."""
    docs, failed = [], []
    try:
        ids = list_ids(session, base_url, section, args.limit, args.delay, args.timeout)
        print(f"{section}: {len(ids)} items", file=sys.stderr)
        for count, sequence_id in enumerate(ids, 1):
            time.sleep(args.delay)
            try:
                article = fetch_article(session, base_url, section, sequence_id, args.timeout)
            except (RuntimeError, requests.RequestException) as exc:
                article = None
                print(f"  error: {exc}", file=sys.stderr)
            if article:
                docs.extend(build_docs(article, section, args.file_labels))
            else:
                failed.append(f"{section}/{sequence_id}")
            if count % 25 == 0 or count == len(ids):
                print(f"  {section}: {count}/{len(ids)}", file=sys.stderr)
        if ids and not args.limit:
            skipped = fetch_skipped(session, base_url, section, ids, args)
            if skipped:
                print(f"  {section}: {len(skipped)} more that the list pages skipped", file=sys.stderr)
                for article in skipped:
                    docs.extend(build_docs(article, section, args.file_labels))
                # Stable sort, so the language records of one item stay together.
                docs.sort(key=lambda doc: int(re.search(r"-(\d+)(-[a-z]+)?$", doc["id"]).group(1)), reverse=True)
    except KeyboardInterrupt:
        print("\ninterrupted, saving what was collected", file=sys.stderr)
        return docs, failed, True
    except (RuntimeError, requests.RequestException) as exc:
        print(f"error: {section}: {exc}", file=sys.stderr)
        failed.append(f"{section} (list)")
    return docs, failed, False


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output-dir", default="fishagriocean",
                        help="directory for the per-section JSON files (default: fishagriocean)")
    parser.add_argument("-s", "--sections", nargs="+", choices=list(SECTIONS), default=list(SECTIONS),
                        metavar="SECTION", help="sections to scrape (default: all): " + ", ".join(SECTIONS))
    parser.add_argument("-n", "--limit", type=int, default=0, help="newest N items per section (default: all)")
    parser.add_argument("--file-labels", choices=["both", "en", "dv", "split"], default="both",
                        help="file labels in publications: both keeps one record with 'English / Dhivehi' labels; "
                             "en or dv keeps one record with that language only; split writes a separate record "
                             "per language, each with its own labels (default: both)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    parser.add_argument("--base-url", default=BASE_URL, help=f"site to scrape (default: {BASE_URL})")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    base_url = args.base_url.rstrip("/")

    total = 0
    all_failed = []
    for section in args.sections:
        docs, failed, interrupted = scrape_section(session, base_url, section, args)
        all_failed.extend(failed)

        path = os.path.join(args.output_dir, section + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        result = {"docs": docs, "totalDocs": len(docs), "hasNextPage": False, "nextPage": None}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"  saved {len(docs)} docs to {path}", file=sys.stderr)
        total += len(docs)
        if interrupted:
            break

    print(f"Saved {total} docs under {args.output_dir}/", file=sys.stderr)
    if all_failed:
        print(f"Could not fetch {len(all_failed)}: {', '.join(all_failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
