#!/usr/bin/env python3
"""Scrape news articles from islamicaffairs.gov.mv through its public WordPress REST API.

Output is a single JSON file shaped like {"docs": [...], "totalDocs": N, "hasNextPage": false,
"nextPage": null}, with article bodies converted to Markdown.
"""

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

BASE_URL = "https://islamicaffairs.gov.mv"
USER_AGENT = "simple-crawler/1.0"
PER_PAGE = 100

# WordPress post type -> REST endpoint, locale of its content, and the source_type to emit.
POST_TYPES = {
    "post": {"endpoint": "posts", "locale": "dv", "source_type": "News"},
    "english_new": {"endpoint": "english_new", "locale": "en", "source_type": "News"},
    "arabic_new": {"endpoint": "arabic_new", "locale": "ar", "source_type": "News"},
}
FILE_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip")
DESCRIPTION_MAX = 300


def get(session, url, params, timeout, retries=3):
    """GET with a few retries on network errors and 5xx responses."""
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp
            error = f"HTTP {resp.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            error = str(exc)
        if attempt == retries:
            raise RuntimeError(f"{url} failed after {retries} attempts: {error}")
        time.sleep(2 * attempt)


def fetch_posts(session, base_url, post_type, limit, since, delay, timeout):
    """Yield raw posts of one type, newest first."""
    url = f"{base_url}/wp-json/wp/v2/{POST_TYPES[post_type]['endpoint']}"
    params = {"per_page": PER_PAGE, "page": 1, "_embed": "wp:featuredmedia", "orderby": "date", "order": "desc"}
    if since:
        params["after"] = f"{since}T00:00:00"

    fetched = 0
    while True:
        resp = get(session, url, params, timeout)
        total = int(resp.headers.get("X-WP-Total", 0))
        total_pages = int(resp.headers.get("X-WP-TotalPages", 0))
        for post in resp.json():
            yield post
            fetched += 1
            if limit and fetched >= limit:
                return
        print(f"  {post_type}: {fetched}/{min(total, limit) if limit else total}", file=sys.stderr)
        if params["page"] >= total_pages:
            return
        params["page"] += 1
        time.sleep(delay)


def to_markdown(content_html):
    body = markdownify(content_html, heading_style="ATX", bullets="-")
    body = body.replace("\xa0", " ")
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def plain_text(fragment):
    return " ".join(BeautifulSoup(fragment, "html.parser").get_text(" ").split())


def describe(post, soup):
    """Use the WordPress excerpt when there is one, otherwise the first paragraph of the body."""
    text = plain_text(post.get("excerpt", {}).get("rendered", ""))
    text = re.sub(r"\s*\[(…|\.\.\.)\]$", "", text)
    if not text:
        text = next((t for t in (" ".join(p.get_text(" ").split()) for p in soup.find_all("p")) if t), "")
    if len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"
    return text


def parse_post(post, post_type, base_url):
    """Reduce a raw WordPress post to the fields the output needs."""
    content_html = post.get("content", {}).get("rendered", "")
    soup = BeautifulSoup(content_html, "html.parser")

    files = []
    for a in soup.find_all("a", href=True):
        link = urljoin(base_url, a["href"].strip())
        if urlparse(link).path.lower().endswith(FILE_EXTENSIONS) and link not in files:
            files.append(link)

    media = post.get("_embedded", {}).get("wp:featuredmedia", [])
    featured_image = media[0].get("source_url") if media else None

    return {
        "wp_id": post["id"],
        "locale": POST_TYPES[post_type]["locale"],
        "source_type": POST_TYPES[post_type]["source_type"],
        "published_at": post["date_gmt"] + "Z",
        "title": html.unescape(post["title"]["rendered"]).strip(),
        "description": describe(post, soup),
        "body": to_markdown(content_html),
        "featured_image": featured_image,
        "files": files,
    }


def translation(article):
    fields = {"title": article["title"]}
    if article["description"]:
        fields["description"] = article["description"]
    fields["body"] = article["body"]
    return fields


def single_doc(article):
    """One record per language, for articles with no known translation."""
    doc = {
        "id": f"article-{article['wp_id']}-{article['locale']}",
        "locale": article["locale"],
        "source_type": article["source_type"],
        "published_at": article["published_at"],
    }
    doc.update(translation(article))
    if article["featured_image"]:
        doc["featuredImage"] = article["featured_image"]
    if article["files"]:
        doc["files"] = article["files"]
    return doc


def bilingual_doc(dv, en):
    """One record holding both languages of the same article."""
    doc = {
        "id": f"article-{dv['wp_id']}",
        "source_type": dv["source_type"],
        "published_at": dv["published_at"],
        "translations": {"en": translation(en), "dv": translation(dv)},
    }
    featured_image = dv["featured_image"] or en["featured_image"]
    if featured_image:
        doc["featuredImage"] = featured_image
    files = dv["files"] + [f for f in en["files"] if f not in dv["files"]]
    if files:
        doc["files"] = files
    return doc


def pair_by_time(dv_articles, en_articles, window_minutes):
    """Match Dhivehi and English articles published within the window of each other.

    The site stores the two languages as unrelated posts, so this is a guess based on publish
    time. A pair is only made when each article is the other's closest match.
    """
    def published(article):
        return datetime.strptime(article["published_at"], "%Y-%m-%dT%H:%M:%SZ")

    def nearest(article, candidates):
        return min(candidates, key=lambda c: abs((published(c) - published(article)).total_seconds()))

    pairs = []
    if not dv_articles or not en_articles:
        return pairs
    for en in en_articles:
        dv = nearest(en, dv_articles)
        gap = abs((published(dv) - published(en)).total_seconds())
        if gap <= window_minutes * 60 and nearest(dv, en_articles) is en:
            pairs.append((dv, en))
    return pairs


def build_docs(articles, pair_within):
    pairs = []
    if pair_within:
        pairs = pair_by_time(
            [a for a in articles if a["locale"] == "dv"],
            [a for a in articles if a["locale"] == "en"],
            pair_within,
        )
    partner = {dv["wp_id"]: en for dv, en in pairs}
    paired_en = {en["wp_id"] for _, en in pairs}

    docs = []
    for article in sorted(articles, key=lambda a: a["published_at"], reverse=True):
        if article["wp_id"] in partner:
            docs.append(bilingual_doc(article, partner[article["wp_id"]]))
        elif article["wp_id"] not in paired_en:
            docs.append(single_doc(article))
    return docs, len(pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="output.json", help="output file (default: output.json)")
    parser.add_argument("-t", "--types", nargs="+", choices=sorted(POST_TYPES), default=sorted(POST_TYPES),
                        help="content types to scrape (default: all)")
    parser.add_argument("-n", "--limit", type=int, default=0, help="newest N articles per type (default: all)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="only articles published after this date")
    parser.add_argument("--pair-within", type=float, default=0, metavar="MINUTES",
                        help="merge Dhivehi and English articles published within this many minutes of "
                             "each other into one bilingual record (default: off)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    parser.add_argument("--base-url", default=BASE_URL, help=f"site to scrape (default: {BASE_URL})")
    args = parser.parse_args()

    if args.since:
        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since must be YYYY-MM-DD")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    base_url = args.base_url.rstrip("/")

    articles = []
    try:
        for post_type in args.types:
            for post in fetch_posts(session, base_url, post_type, args.limit, args.since, args.delay, args.timeout):
                articles.append(parse_post(post, post_type, base_url))
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\ninterrupted, saving what was collected", file=sys.stderr)
    except (RuntimeError, requests.RequestException) as exc:
        print(f"error: {exc}", file=sys.stderr)
        if not articles:
            return 1
        print("saving what was collected", file=sys.stderr)

    docs, pair_count = build_docs(articles, args.pair_within)
    result = {"docs": docs, "totalDocs": len(docs), "hasNextPage": False, "nextPage": None}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    summary = f"Saved {len(docs)} docs to {args.output}"
    if args.pair_within:
        summary += f" ({pair_count} bilingual, {len(docs) - pair_count} single-language)"
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
