#!/usr/bin/env python3
"""Scrape the Media section of education.gov.mv (Ministry of Education, Higher Education and Skills
Development): news, press releases and galleries. Proactive disclosures (Right to Information)
are not included.

The site has no public JSON API; pages are rendered on the server. Each list is walked page by
page in English and Dhivehi. Every section is written to its own folder in files of 100 records,
each shaped like {"docs": [...], "totalDocs": N, "hasNextPage": ..., "nextPage": ...}, with one
bilingual record per item and bodies converted to Markdown.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

BASE_URL = "https://education.gov.mv"
USER_AGENT = "simple-crawler/1.0"
LOCALES = ("en", "dv")
DESCRIPTION_MAX = 300
CHUNK_SIZE = 100
THAANA = re.compile("[ހ-޿]")


def get(session, url, timeout, retries=3):
    """GET a page; returns None for a 404."""
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


def list_articles(session, locale, limit, delay, timeout):
    """Walk the news list of one language. Returns {article id: path}, newest first."""
    link = re.compile(rf"^/{locale}/news/(\d+)/")
    paths = {}
    page = 1
    while True:
        html = get(session, f"{BASE_URL}/{locale}/news?page={page}", timeout) or ""
        found = {}
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=link):
            found.setdefault(int(link.match(a["href"]).group(1)), a["href"])
        # Every page also carries a "latest news" sidebar, so stop once a page adds nothing new.
        new = {article_id: path for article_id, path in found.items() if article_id not in paths}
        if not new:
            break
        paths.update(new)
        if limit and len(paths) >= limit:
            break
        page += 1
        time.sleep(delay)
    ids = sorted(paths, reverse=True)
    return {article_id: paths[article_id] for article_id in (ids[:limit] if limit else ids)}


def parse_article(html):
    """Pull the title, date, body and images out of an article page."""
    soup = BeautifulSoup(html, "html.parser")
    content = soup.find("div", class_="article-content")
    title = soup.find("h1")
    if not content or not title:
        return None

    date = soup.find("span", class_="text-subsubheadingPrimary")
    featured = soup.find("meta", property="og:image")
    # The photo carousel sits between the title and the body.
    images = []
    for img in content.find_all_previous("img"):
        if title in img.find_all_previous("h1") and img.get("src") and img["src"] not in images:
            images.insert(0, img["src"])

    paragraphs = [" ".join(p.get_text(" ").split()) for p in content.find_all("p")]
    description = next((text for text in paragraphs if text), "")
    if len(description) > DESCRIPTION_MAX:
        description = description[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"

    body = markdownify(str(content), heading_style="ATX", bullets="-").replace("\xa0", " ")
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return {
        "title": " ".join(title.get_text(" ").split()),
        "date": date.get_text(strip=True) if date else "",
        "description": description,
        "body": re.sub(r"\n{3,}", "\n\n", body).strip(),
        "featured": featured["content"] if featured and featured.get("content") else None,
        "images": images,
    }


def published_at(english_date):
    """'08 October 2026' -> '2026-10-08T00:00:00Z'. The site shows the day only."""
    try:
        return datetime.strptime(english_date, "%d %B %Y").strftime("%Y-%m-%dT00:00:00Z")
    except ValueError:
        return ""


def build_doc(article_id, pages):
    """Combine the language versions of one article into a record."""
    doc = {"id": f"moe-news-{article_id}", "source_type": "News", "published_at": "", "translations": {}}
    featured = next((page["featured"] for page in pages.values() if page["featured"]), None)

    for locale in LOCALES:
        page = pages.get(locale)
        # An article without an English version shows its Dhivehi text on the English page.
        if not page or (locale == "en" and THAANA.search(page["title"])):
            continue
        gallery = "\n\n".join(f"![]({url})" for url in page["images"] if url != featured and url not in page["body"])
        fields = {"title": page["title"]}
        if page["description"]:
            fields["description"] = page["description"]
        fields["body"] = "\n\n".join(part for part in (page["body"], gallery) if part)
        doc["translations"][locale] = fields

    if "en" in pages:
        doc["published_at"] = published_at(pages["en"]["date"])
    if featured:
        doc["featuredImage"] = featured
    return doc


def scrape_news(session, args):
    docs, failed = [], []
    paths = {locale: list_articles(session, locale, args.limit, args.delay, args.timeout) for locale in LOCALES}
    ids = sorted(set(paths["en"]) | set(paths["dv"]), reverse=True)
    print(f"news: {len(ids)} articles listed ({len(paths['en'])} en, {len(paths['dv'])} dv)", file=sys.stderr)

    for count, article_id in enumerate(ids, 1):
        pages = {}
        for locale in LOCALES:
            # Both languages share one slug, so a missing list entry can still be tried.
            path = paths[locale].get(article_id) or re.sub(
                r"^/\w+/", f"/{locale}/", paths["en" if locale == "dv" else "dv"][article_id])
            time.sleep(args.delay)
            try:
                html = get(session, BASE_URL + path, args.timeout)
            except RuntimeError as exc:
                print(f"  error: {exc}", file=sys.stderr)
                html = None
            page = parse_article(html) if html else None
            if page:
                pages[locale] = page
        doc = build_doc(article_id, pages)
        if doc["translations"]:
            docs.append(doc)
        else:
            failed.append(f"news/{article_id}")
        if count % 25 == 0 or count == len(ids):
            print(f"  news: {count}/{len(ids)}", file=sys.stderr)
    return docs, failed


def walk_list(session, path, locale, parse_items, args):
    """Collect {item id: item} from every page of a list, stopping at the first page with nothing new."""
    items = {}
    page = 1
    while True:
        html = get(session, f"{BASE_URL}/{locale}/{path}?page={page}", args.timeout) or ""
        new = {key: item for key, item in parse_items(BeautifulSoup(html, "html.parser"), locale).items()
               if key not in items}
        if not new:
            return items
        items.update(new)
        page += 1
        time.sleep(args.delay)


def press_release_items(soup, locale):
    items = {}
    for a in soup.find_all("a", attrs={"wire:click": re.compile(r"^download\(\d+\)$")}):
        title, date = a.find("h2"), a.find(attrs={"title": "Published date"})
        items[int(re.search(r"\d+", a["wire:click"]).group())] = {
            "title": " ".join(title.get_text(" ").split()) if title else "",
            "date": date.get_text(strip=True) if date else "",
            "file": a.get("href", ""),
        }
    return items


def scrape_press_releases(session, args):
    lists = {locale: walk_list(session, "press-releases", locale, press_release_items, args) for locale in LOCALES}
    ids = sorted(set(lists["en"]) | set(lists["dv"]), reverse=True)
    print(f"press-releases: {len(ids)} listed", file=sys.stderr)
    docs = []
    for item_id in ids:
        versions = {locale: lists[locale][item_id] for locale in LOCALES if item_id in lists[locale]}
        any_version = next(iter(versions.values()))
        doc = {
            "id": f"moe-press-release-{item_id}",
            "source_type": "Press Release",
            "published_at": published_at(versions["en"]["date"]) if "en" in versions else "",
            "translations": {locale: {"title": item["title"], "body": ""} for locale, item in versions.items()
                             if item["title"] and not (locale == "en" and THAANA.search(item["title"]))},
        }
        if any_version["file"]:
            # The site links files with raw spaces in the name.
            doc["files"] = [quote(any_version["file"], safe=":/")]
        docs.append(doc)
    return docs, []


def gallery_items(soup, locale):
    items = {}
    for a in soup.find_all("a", href=re.compile(rf"^/{locale}/galleries/\d+$")):
        title, date = a.find("h5"), a.find("small", class_=False)
        if not title:
            continue
        items[int(a["href"].rsplit("/", 1)[1])] = {
            "title": " ".join(title.get_text(" ").split()),
            "date": " ".join(date.get_text(" ").split()) if date else "",
        }
    return items


def scrape_galleries(session, args):
    lists = {locale: walk_list(session, "galleries", locale, gallery_items, args) for locale in LOCALES}
    ids = sorted(set(lists["en"]) | set(lists["dv"]), reverse=True)
    print(f"galleries: {len(ids)} listed", file=sys.stderr)
    docs, failed = [], []
    for item_id in ids:
        time.sleep(args.delay)
        try:
            html = get(session, f"{BASE_URL}/en/galleries/{item_id}", args.timeout)
        except RuntimeError as exc:
            print(f"  error: {exc}", file=sys.stderr)
            html = None
        if not html:
            failed.append(f"galleries/{item_id}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        photos = []
        for img in soup.find_all("img", src=re.compile(r"/storage/img/gallery/")):
            if img["src"] not in photos:
                photos.append(img["src"])
        body = "\n\n".join(f"![]({url})" for url in photos)
        versions = {locale: lists[locale][item_id] for locale in LOCALES if item_id in lists[locale]}
        doc = {
            "id": f"moe-gallery-{item_id}",
            "source_type": "Gallery",
            "published_at": published_at(versions["en"]["date"]) if "en" in versions else "",
            "translations": {locale: {"title": item["title"], "body": body} for locale, item in versions.items()
                             if item["title"] and not (locale == "en" and THAANA.search(item["title"]))},
        }
        if photos:
            doc["featuredImage"] = photos[0]
        docs.append(doc)
    return docs, failed


SECTIONS = {"news": scrape_news, "press-releases": scrape_press_releases, "galleries": scrape_galleries}


def write_chunks(docs, folder, name):
    """Write docs newest first in files of CHUNK_SIZE."""
    docs.sort(key=lambda doc: doc["published_at"], reverse=True)
    os.makedirs(folder, exist_ok=True)
    pages = max(1, -(-len(docs) // CHUNK_SIZE))
    for page in range(1, pages + 1):
        result = {
            "docs": docs[(page - 1) * CHUNK_SIZE:page * CHUNK_SIZE],
            "totalDocs": len(docs),
            # Each file is imported on its own, so none of them points at a next page.
            "hasNextPage": False,
            "nextPage": None,
        }
        with open(os.path.join(folder, f"{name}-{page}.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  saved {len(docs)} docs to {folder}/ in {pages} file(s)", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output-dir", default="education/media",
                        help="directory with one folder per section (default: education/media)")
    parser.add_argument("-s", "--sections", nargs="+", choices=list(SECTIONS), default=list(SECTIONS),
                        help="sections to scrape (default: all)")
    parser.add_argument("-n", "--limit", type=int, default=0, help="newest N news articles only (default: all)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    all_failed = []
    for section in args.sections:
        try:
            docs, failed = SECTIONS[section](session, args)
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
