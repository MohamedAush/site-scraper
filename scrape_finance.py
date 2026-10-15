#!/usr/bin/env python3
"""Scrape the Media section of finance.gov.mv (Ministry of Finance and Public Enterprises).

Covers press releases, news, announcements, speeches, resources, galleries and press materials;
Right to Information is left out. The site is English only and has no public API, so pages are
read as HTML. Each section is written to its own folder in files of 100 records shaped like
{"docs": [...], "totalDocs": N, "hasNextPage": false, "nextPage": null}, with bodies converted to
Markdown.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

BASE_URL = "https://finance.gov.mv"
USER_AGENT = "simple-crawler/1.0"
CHUNK_SIZE = 100
DESCRIPTION_MAX = 300

# Section -> how it is listed, its id prefix and the source_type to emit.
#   pages: a paginated list of items, each with its own page
#   resources: a paginated list of documents with a label and date
#   documents: one page of grouped document links, without dates
SECTIONS = {
    "press-releases": {"kind": "pages", "path": "/media/press-releases", "prefix": "mof-press-release",
                       "source_type": "Press Release"},
    "news": {"kind": "pages", "path": "/media/news", "prefix": "mof-news", "source_type": "News"},
    "announcements": {"kind": "pages", "path": "/media/announcements", "prefix": "mof-announcement",
                      "source_type": "Announcement"},
    "speeches": {"kind": "documents", "path": "/speeches", "prefix": "mof-speech", "source_type": "Speech"},
    "resources": {"kind": "resources", "path": "/library-resources", "prefix": "mof-resource",
                  "source_type": "Resource"},
    "galleries": {"kind": "pages", "path": "/galleries", "prefix": "mof-gallery", "source_type": "Gallery"},
    "press-materials": {"kind": "documents", "path": "/press-materials", "prefix": "mof-press-material",
                        "source_type": "Press Material"},
}


def get(session, url, timeout, retries=3):
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code == 404:
                return None
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.text
            error = f"HTTP {resp.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            error = str(exc)
        if attempt == retries:
            raise RuntimeError(f"{url} failed after {retries} attempts: {error}")
        time.sleep(2 * attempt)


def clean_url(url):
    """Point file and image links at their real location on finance.gov.mv."""
    url = (url or "").strip()
    # Document viewer embeds carry the real file in their "url" parameter.
    if "docs.google.com/gview" in url:
        url = parse_qs(urlparse(url).query).get("url", [url])[0]
    url = urljoin(BASE_URL + "/", url)
    parsed = urlparse(url)
    path = parsed.path
    # Resized copies (/imgfly/images/NAME?w=...) come from /public/attachments/NAME.
    match = re.match(r"^/imgfly/images/(.+)$", path)
    if match:
        path = "/public/attachments/" + match.group(1)
        parsed = parsed._replace(query="")
    # Old gallery pages still point at the developer's machine, or at the www host.
    if parsed.hostname in ("localhost", "www.finance.gov.mv", "finance.gov.mv"):
        parsed = parsed._replace(scheme="https", netloc="finance.gov.mv")
    return parsed._replace(path=quote(unquote(path))).geturl()


def published_at(text):
    try:
        return datetime.strptime(" ".join(text.split()), "%d %B %Y").strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        return None


def describe(soup):
    text = next((t for t in (" ".join(p.get_text(" ").split()) for p in soup.find_all("p")) if t), "")
    if len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"
    return text


def to_markdown(html):
    body = markdownify(html, heading_style="ATX", bullets="-").replace("\xa0", " ")
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def parse_item_page(html):
    """Title, date, featured image, body and attached files from a news/announcement/gallery page."""
    soup = BeautifulSoup(html, "html.parser")
    column = soup.find("div", class_="lg:col-span-9")
    if not column:
        return None
    heading = column.find("div", recursive=False)
    title = " ".join("".join(heading.find_all(string=True, recursive=False)).split())
    date = heading.find("span", class_="text-sm")

    featured, files, photos = None, [], []
    parts = []
    for block in heading.find_next_siblings():
        figure = block if block.name == "figure" else None
        if figure and figure.find("img"):
            featured = featured or clean_url(figure.find("img").get("src"))
            continue
        for frame in block.find_all("iframe"):
            src = frame.get("src") or ""
            if "gview" in src or re.search(r"\.(pdf|docx?|xlsx?|pptx?)\b", src, re.I):
                files.append(clean_url(src))
            elif src:
                parts.append(f"<{src if src.startswith('http') else 'https:' + src}>")
            frame.decompose()
        if "grid" in (block.get("class") or []):
            # Gallery photos, one per paragraph.
            photos.extend(clean_url(img.get("src")) for img in block.find_all("img"))
            parts.append("\n\n".join(f"![]({url})" for url in photos))
            continue
        for img in block.find_all("img"):
            img["src"] = clean_url(img.get("src"))
        for a in block.find_all("a", href=True):
            a["href"] = clean_url(a["href"]) if "/attachments/" in a["href"] else a["href"]
        if block.get_text(strip=True) or block.find("img"):
            parts.append(to_markdown(str(block)))

    content = BeautifulSoup("".join(str(b) for b in heading.find_next_siblings()), "html.parser")
    return {
        "title": title,
        "date": published_at(date.get_text()) if date else None,
        "description": describe(content),
        "body": "\n\n".join(part for part in parts if part),
        "featured": featured or (photos[0] if photos else None),
        "files": list(dict.fromkeys(files)),
    }


def item_doc(section, key, title, date, body="", description="", featured=None, files=None):
    doc = {
        "id": f"{SECTIONS[section]['prefix']}-{key}",
        "source_type": SECTIONS[section]["source_type"],
        "published_at": date,
        "translations": {"en": {"title": title}},
    }
    if description:
        doc["translations"]["en"]["description"] = description
    doc["translations"]["en"]["body"] = body
    if featured:
        doc["featuredImage"] = featured
    if files:
        doc["files"] = files
    return doc


def list_pages(session, section, args, parse_page):
    """Walk ?page=1,2,... collecting {key: value} until a page adds nothing new."""
    found = {}
    page = 1
    while True:
        html = get(session, f"{BASE_URL}{SECTIONS[section]['path']}?page={page}", args.timeout) or ""
        new = {k: v for k, v in parse_page(BeautifulSoup(html, "html.parser")).items() if k not in found}
        if not new:
            return found
        found.update(new)
        if args.limit and len(found) >= args.limit:
            return dict(list(found.items())[:args.limit])
        page += 1
        time.sleep(args.delay)


def scrape_pages(session, section, args):
    path = SECTIONS[section]["path"]
    link = re.compile(rf"^{re.escape(BASE_URL + path)}/([^/?#]+)$")

    def parse_page(soup):
        # Only the main list; the sidebar's "Latest" links repeat items from the same section.
        main = soup.find("div", class_="lg:col-span-9") or soup
        return {link.match(a["href"]).group(1): a["href"] for a in main.find_all("a", href=link)}

    urls = list_pages(session, section, args, parse_page)
    print(f"{section}: {len(urls)} listed", file=sys.stderr)
    docs, failed = [], []
    for count, (slug, url) in enumerate(urls.items(), 1):
        time.sleep(args.delay)
        try:
            html = get(session, url, args.timeout)
        except RuntimeError as exc:
            print(f"  error: {exc}", file=sys.stderr)
            html = None
        item = parse_item_page(html) if html else None
        if item and item["title"]:
            docs.append(item_doc(section, slug, item["title"], item["date"], item["body"], item["description"],
                                 item["featured"], item["files"]))
        else:
            failed.append(f"{section}/{slug}")
        if count % 50 == 0 or count == len(urls):
            print(f"  {section}: {count}/{len(urls)}", file=sys.stderr)
    return docs, failed


def file_key(url):
    return os.path.splitext(os.path.basename(urlparse(url).path))[0]


def scrape_resources(session, section, args):
    def parse_page(soup):
        items = {}
        for label in soup.find_all("span", class_="item--label"):
            box = label.parent
            a = box.find("a", href=True)
            date = box.find("span", class_="text-sm")
            if a:
                url = clean_url(a["href"])
                items[file_key(url)] = {
                    "title": " ".join(a.get_text(" ").split()),
                    "date": published_at(date.get_text()) if date else None,
                    "url": url,
                }
        return items

    items = list_pages(session, section, args, parse_page)
    print(f"{section}: {len(items)} listed", file=sys.stderr)
    return [item_doc(section, key, item["title"], item["date"], files=[item["url"]])
            for key, item in items.items()], []


def scrape_documents(session, section, args):
    html = get(session, BASE_URL + SECTIONS[section]["path"], args.timeout) or ""
    soup = BeautifulSoup(html, "html.parser")
    items = {}
    for group in soup.find_all("div", class_="article--accordion"):
        for a in group.find_all("a", href=re.compile(r"/attachments/")):
            title = " ".join(a.get_text(" ").split())
            url = clean_url(a["href"])
            if title and file_key(url) not in items:
                items[file_key(url)] = (title, url)
    print(f"{section}: {len(items)} listed", file=sys.stderr)
    # These pages give no dates, so published_at is null.
    return [item_doc(section, key, title, None, files=[url]) for key, (title, url) in items.items()], []


SCRAPERS = {"pages": scrape_pages, "resources": scrape_resources, "documents": scrape_documents}


def write_chunks(docs, folder, name):
    docs.sort(key=lambda doc: doc["published_at"] or "", reverse=True)
    os.makedirs(folder, exist_ok=True)
    pages = max(1, -(-len(docs) // CHUNK_SIZE))
    for page in range(1, pages + 1):
        # Each file is imported on its own, so none of them points at a next page.
        result = {"docs": docs[(page - 1) * CHUNK_SIZE:page * CHUNK_SIZE], "totalDocs": len(docs),
                  "hasNextPage": False, "nextPage": None}
        with open(os.path.join(folder, f"{name}-{page}.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  saved {len(docs)} docs to {folder}/ in {pages} file(s)", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output-dir", default="finance/media",
                        help="directory with one folder per section (default: finance/media)")
    parser.add_argument("-s", "--sections", nargs="+", choices=list(SECTIONS), default=list(SECTIONS),
                        help="sections to scrape (default: all)")
    parser.add_argument("-n", "--limit", type=int, default=0, help="first N items per section (default: all)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    all_failed = []
    for section in args.sections:
        try:
            docs, failed = SCRAPERS[SECTIONS[section]["kind"]](session, section, args)
        except KeyboardInterrupt:
            print("\ninterrupted", file=sys.stderr)
            return 1
        except (RuntimeError, requests.RequestException) as exc:
            print(f"error: {section}: {exc}", file=sys.stderr)
            all_failed.append(section)
            continue
        write_chunks(docs, os.path.join(args.output_dir, section), section)
        all_failed.extend(failed)

    if all_failed:
        print(f"Could not read {len(all_failed)}: {', '.join(all_failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
