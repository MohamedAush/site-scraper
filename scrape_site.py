#!/usr/bin/env python3
"""Scrape any website into one of the sample JSON formats.

Give it a listing page (every item on it is followed, across pages) or a single item page, and the
format to produce: publications, newsroom, downloads, gallery, projects or video. Structure (titles,
dates, bodies, images, files, videos) is read from the HTML. Fields the HTML has no fixed place for
(project sector, funding and timeline, download types, categories) are filled in by Claude when
ANTHROPIC_API_KEY is set; --no-llm skips that. The Dhivehi version of each page is found through the
site's language switcher, or by pairing with the listing given in --dv-url.

Examples:
  scrape_site.py https://finance.gov.mv/media/news --format publications --limit 5
  scrape_site.py https://example.gov.mv/projects --format projects --dv-url https://example.gov.mv/dv/projects
  scrape_site.py https://example.gov.mv/videos --format video --category Events --no-llm
"""

import argparse
import copy
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from html import escape
from urllib.parse import parse_qs, quote, unquote, urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify

from scrape_formats import FORMATS, Claude, build_docs, checkpoint, envelope, log, missing_fields

USER_AGENT = "simple-crawler/1.0"
CHUNK_SIZE = 100
DESCRIPTION_MAX = 300
THAANA = re.compile("[ހ-޿]")
FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|csv|zip|rar|odt|ods)$", re.I)
VIDEO_URL = re.compile(r"(youtube\.com/(watch|embed|shorts)|youtu\.be/|vimeo\.com/|facebook\.com/.+/videos/)", re.I)
IMAGE_SKIP = re.compile(r"(logo|icon|avatar|spinner|placeholder|emblem|flag|sprite|\.svg$)", re.I)
BOILERPLATE = re.compile(r"(^|[\s_-])(nav|navbar|menu|sidebar|breadcrumbs?|share|sharing|social|related|cookie|"
                         r"footer|header|pagination|pager|widget|comments?|subscribe|search)([\s_-]|$)", re.I)
SHARE_LINK = re.compile(r"(facebook\.com/sharer|twitter\.com/(intent|share)|x\.com/intent|linkedin\.com/share|"
                        r"wa\.me/|api\.whatsapp\.com/send|t\.me/share|telegram\.me/share|viber://|mailto:\?)", re.I)
# Page builders put "widget" in every block's class (elementor-widget-container); only the rest of the name counts.
BUILDER_PREFIXES = ("elementor", "et_pb", "fl-", "vc_")
# Photo gallery blocks (Elementor, WordPress); they hold the item's own photos, not "more galleries" thumbnails.
GALLERY_BLOCKS = ".elementor-gallery__container, .elementor-image-gallery, .wp-block-gallery, div.gallery[id^=gallery-]"
SIZED = re.compile(r"(-\d+x\d+|-scaled)(\.\w+)$")  # WordPress copies of one picture: photo-768x512.jpg
REMOVE_TAGS =["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "button", "template"]
NEXT_TEXT = re.compile(r"^\s*(next|next page|older|older posts|more|»|›|>|→|ކުރިއަށް)\s*[»›>→]?\s*$", re.I)
# CSS selectors given on the command line for sites the general rules misread.
SELECTORS = {"title": None, "content": None}
MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)}
MONTH_RE = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|" \
           r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)"), lambda m: (m[1], m[2], m[3])),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+{MONTH_RE},?\s+(\d{{4}})\b", re.I), lambda m: (m[3], m[2], m[1])),
    (re.compile(rf"\b{MONTH_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), lambda m: (m[3], m[1], m[2])),
    (re.compile(r"(\d{1,2})\s+([ހ-޿]+)،?\s+(\d{4})"), lambda m: (m[3], m[2], m[1])),
]
# Dhivehi month names by their first three letters, as spellings vary after that (އޯގަސްޓު as well as އޮގަސްޓު).
DV_MONTHS = {name: i for i, names in enumerate(
    [["ޖަނ", "ޖެނ"], ["ފެބ"], ["މާރ"], ["އޭޕ", "އެޕ"], ["މޭ"], ["ޖޫނ"], ["ޖުލ"], ["އޮގ", "އޯގ"], ["ސެޕ"], ["އޮކ"],
     ["ނޮވ"], ["ޑިސ"]], 1) for name in names}


# ---------------------------------------------------------------------------------------------- fetching

def get(session, url, timeout, retries=3):
    """Page HTML, or None if it does not exist."""
    checkpoint()
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            if resp.status_code in (404, 410):
                return None
            if resp.status_code < 500:
                resp.raise_for_status()
                # Without a charset in the header requests assumes Latin-1, which garbles Thaana.
                if "charset" not in resp.headers.get("content-type", "").lower():
                    resp.encoding = "utf-8"
                return resp.text
            error = f"HTTP {resp.status_code}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            error = str(exc)
        except requests.HTTPError as exc:
            raise RuntimeError(f"{url}: {exc}")
        if attempt == retries:
            raise RuntimeError(f"{url} failed after {retries} attempts: {error}")
        time.sleep(2 * attempt)


def clean_url(url, base):
    """Absolute URL, with document-viewer embeds unwrapped to the real file and the path quoted."""
    url = (url or "").strip()
    if not url or url.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
        return None
    if "docs.google.com/gview" in url or "docs.google.com/viewer" in url:
        url = parse_qs(urlparse(url).query).get("url", [url])[0]
    if url.startswith("//"):
        url = "https:" + url
    url = urldefrag(urljoin(base, url))[0]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return None
    return parsed._replace(path=quote(unquote(parsed.path))).geturl()


def same_site(url, base):
    host = lambda u: re.sub(r"^www\.", "", (urlparse(u).hostname or "").lower())
    return host(url) == host(base)


# ---------------------------------------------------------------------------------------------- page parts

def text_of(node):
    return " ".join(node.get_text(" ").split()) if node else ""


def language(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    return "dv" if sum(1 for c in letters if THAANA.match(c)) > len(letters) / 3 else "en"


def strip_boilerplate(soup):
    """A copy of the page without scripts, menus, headers, footers, sidebars and share bars."""
    soup = copy.copy(soup)
    for tag in soup(REMOVE_TAGS):
        tag.decompose()
    total = len(text_of(soup.body or soup)) or 1
    for el in soup.find_all(True):
        if getattr(el, "decomposed", False) or el.attrs is None:
            continue
        classes = [c.replace("widget", "") if c.startswith(BUILDER_PREFIXES) else c for c in el.get("class") or []]
        names = " ".join(classes) + " " + (el.get("id") or "") + " " + (el.get("role") or "")
        # Only small blocks: a page wrapper called "page-header-wrap" must not take the content with it.
        if BOILERPLATE.search(names) and len(text_of(el)) < total * 0.4:
            el.decompose()
    return soup


def main_content(soup):
    """The element holding the page's own content."""
    stripped = strip_boilerplate(soup)
    articles = [a for a in stripped.find_all("article") if len(text_of(a)) > 200]
    if len(articles) == 1:
        return articles[0]
    main = stripped.find("main") or stripped.find(attrs={"role": "main"})
    scope = main or stripped.body or stripped
    # Readability-style: the block whose paragraphs hold the most text.
    scores = {}
    for p in scope.find_all(["p", "li"] if not scope.find("p") else ["p"]):
        length = len(text_of(p))
        if length < 25:
            continue
        for depth, parent in enumerate([p.parent, p.parent.parent if p.parent else None]):
            if parent is not None:
                scores[id(parent)] = (scores.get(id(parent), (0, parent))[0] + length / (depth + 1), parent)
    if scores:
        best = max(scores.values(), key=lambda item: item[0])[1]
        # Climb while the parent adds images or headings but not much more text (galleries, figures).
        while best.parent is not None and best is not scope and len(text_of(best.parent)) < len(text_of(best)) * 1.5:
            best = best.parent
        return best
    return scope


def describe(soup):
    text = next((t for t in (" ".join(p.get_text(" ").split()) for p in soup.find_all("p")) if t), "")
    if len(text) > DESCRIPTION_MAX:
        text = text[:DESCRIPTION_MAX].rsplit(" ", 1)[0] + "…"
    return text


def to_markdown(html):
    body = markdownify(html, heading_style="ATX", bullets="-").replace("\xa0", " ")
    body = "\n".join(line.rstrip() for line in body.splitlines())
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def label_for(url):
    name = os.path.basename(unquote(urlparse(url).path))
    stem = os.path.splitext(name)[0]
    # Drop upload timestamps appended by the CMS, e.g. "-1791778465021" (one or more).
    stem = re.sub(r"(-\d{10,})+$", "", stem)
    stem = re.sub(r"[_\s]+", " ", stem).strip()
    return stem or name


# A "New" or status badge inside a heading: <h1>Title <span class="status-box-new">New</span></h1>
BADGE = re.compile(r"(^|[\s_-])(badge|new|new-post|status|label|tag|pill)([\s_-]|$)", re.I)


def heading_text(heading):
    heading = copy.copy(heading)
    for el in heading.find_all(True):
        if BADGE.search(" ".join(el.get("class") or [])) and len(text_of(el).split()) <= 2:
            el.decompose()
    return text_of(heading)


def find_title(soup, content):
    meta = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "twitter:title"})
    og = " ".join((meta.get("content") or "").split()) if meta else ""
    # A heading that only repeats a menu item ("Gallery", "News") names the section, not the item.
    menu = {text_of(a).lower() for a in soup.find_all("a")}
    site = soup.find("meta", property="og:site_name")
    site = (site.get("content") or "").strip().lower() if site else ""
    if site and og.lower().startswith(site):
        og = og[len(site):].lstrip(" -|–—:")
    elif site and og.lower().endswith(site):
        og = og[:-len(site)].rstrip(" -|–—:")
    # A shortened og:title ("Grant application fo...") only gives the start of the title.
    cut = re.search(r"\s*(\.\.\.|…)$", og)
    og_start, og = (og[:cut.start()], "") if cut else ("", og)
    page_title = text_of(soup.title)

    def confirmed(heading):
        """The heading, if the page's own title data names it; without a trailing badge ("New") it lacks."""
        words = heading.split()
        texts = [heading] + ([" ".join(words[:-1])] if len(words) > 3 else [])
        if og or og_start:
            return (next((t for t in texts if og and t in og), None)
                    or next((t for t in texts if og_start and t.startswith(og_start)), None))
        # <title> also holds the site's name, so it only decides when there is no og:title at all.
        return next((t for t in texts if t in page_title), None)

    home = lambda h: h.find_parent("a", href=lambda u: urlparse(u).path.strip("/") in ("", "en", "dv"))
    candidates = (content.find_all("h1") if content else []) + soup.find_all("h1")
    # The site's own name sits in an h1 inside the logo link or page header on many sites.
    headings = [heading_text(h) for h in candidates if not home(h) and not h.find_parent(["header", "nav", "footer"])]
    # A "latest news" sidebar links to the current item too, so its title also shows up as a link.
    if og in headings:
        return og
    headings = [h for h in dict.fromkeys(headings) if h and h.lower() not in menu and h.lower() != site]
    for heading in headings:
        if confirmed(heading):
            return confirmed(heading)
    if headings:
        return headings[0]
    # No h1: an h2 or h3 is the title only when the page's title data confirms it (others name page sections).
    for h in soup.find_all(["h2", "h3"]):
        if not h.find_parent(["header", "nav", "footer"]) and confirmed(heading_text(h)):
            return confirmed(heading_text(h))
    if og:
        section, _, rest = og.partition(": ")
        return rest if rest and section.lower() in menu else re.split(r"\s+[|–—]\s+", og)[0]
    parts = [part for part in re.split(r"\s+[|–—-]\s+", page_title) if part]
    if og_start:
        return next((part for part in parts if part.startswith(og_start)), og_start)
    return parts[0] if parts else ""


def parse_date(text):
    for pattern, parts in DATE_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        year, month, day = parts(match)
        if month.isdigit():
            month = int(month)
        elif THAANA.match(month):
            month = next((v for k, v in DV_MONTHS.items() if month.startswith(k)), None)
        else:
            month = next(v for k, v in MONTHS.items() if k.startswith(month.lower()[:3]))
        if not month:
            continue
        try:
            return datetime(int(year), month, int(day)).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def date_only(text):
    """True for a line that is just a date, such as "01 October 2026" or "Posted: Oct 1, 2026"."""
    if not text or len(text) > 50 or not parse_date(text):
        return False
    for pattern, _ in DATE_PATTERNS:
        text = pattern.sub("", text)
    return len(re.sub(r"(posted|published|date|on|at|\d{1,2}:\d{2}\s*(am|pm)?|[\W_])", "", text, flags=re.I)) < 3


def find_date(soup, content):
    for attrs in ({"property": "article:published_time"}, {"itemprop": "datePublished"},
                  {"name": "date"}, {"name": "publish-date"}, {"property": "og:published_time"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and parse_date(meta.get("content", "")):
            return parse_date(meta["content"])
    for node in [content, soup.find("h1")]:
        if not node:
            continue
        stamp = node.find("time") if node.name != "h1" else None
        if stamp and parse_date(stamp.get("datetime", "") + " " + text_of(stamp)):
            return parse_date(stamp.get("datetime", "") + " " + text_of(stamp))
    h1 = soup.find("h1")
    nearby = [h1.parent, h1.parent.parent] if h1 and h1.parent and h1.parent.parent else []
    for node in nearby + [content]:
        found = parse_date(text_of(node)[:600]) if node else None
        if found:
            return found
    return None


def image_url(img, base):
    for attr in ("data-src", "data-lazy-src", "data-original", "src"):
        url = clean_url(img.get(attr), base)
        if url:
            return url
    srcset = (img.get("srcset") or img.get("data-srcset") or "").split(",")
    return clean_url(srcset[-1].strip().split(" ")[0], base) if srcset[0] else None


def find_images(content, base):
    images = []
    for node in content if isinstance(content, list) else [content]:
        for img in node.find_all("img"):
            url = image_url(img, base)
            small = any((img.get(dim) or "").isdigit() and int(img.get(dim)) < 80 for dim in ("width", "height"))
            if url and not small and not IMAGE_SKIP.search(urlparse(url).path):
                images.append({"url": url, "alt": (img.get("alt") or "").strip()})
        # Gallery links that open the full-size picture.
        for a in node.find_all("a", href=re.compile(r"\.(jpe?g|png|webp|gif)(\?|$)", re.I)):
            url = clean_url(a["href"], base)
            if url:
                images.append({"url": url, "alt": (a.get("title") or "").strip()})
    unique = {}
    for image in images:
        key = SIZED.sub(r"\2", image["url"])
        if key not in unique:
            unique[key] = image
        elif SIZED.search(unique[key]["url"]) and not SIZED.search(image["url"]):
            unique[key] = {**image, "alt": unique[key]["alt"] or image["alt"]}  # the full-size copy wins
    return list(unique.values())


def find_files(content, base):
    files = {}
    for el in content.find_all(["a", "iframe", "embed", "object"]):
        url = clean_url(el.get("href") or el.get("src") or el.get("data"), base)
        if url and FILE_EXT.search(urlparse(url).path):
            label = text_of(el) if el.name == "a" else (el.get("title") or "")
            if not label or label.lower() in ("download", "view", "here", "click here", "pdf", "open"):
                label = label_for(url)
            files.setdefault(url, label)
    return [{"label": label, "url": url} for url, label in files.items()]


def find_videos(content, base):
    videos = []
    for el in content.find_all(["iframe", "a", "video", "source"]):
        url = clean_url(el.get("src") or el.get("href") or el.get("data-src"), base)
        if not url:
            continue
        if VIDEO_URL.search(url):
            match = re.search(r"youtube(?:-nocookie)?\.com/embed/([\w-]{11})", url)
            videos.append(f"https://www.youtube.com/watch?v={match.group(1)}" if match else url)
        elif el.name in ("video", "source") and re.search(r"\.(mp4|webm|mov)(\?|$)", url, re.I):
            videos.append(url)
    return list(dict.fromkeys(videos))


def clean_body(body, title):
    """The content without what is already its own field: title, date line, share links, lead photo."""
    for a in body.find_all("a", href=SHARE_LINK):
        a.decompose()
    for el in body.find_all(True):
        if getattr(el, "decomposed", False) or el.attrs is None or el.find(["img", "iframe", "video"]):
            continue
        text = text_of(el)
        if text == title or date_only(text):
            el.decompose()
    for node in body.find_all(string=True):
        if title and " ".join(node.split()) == title:
            node.extract()
    # A photo that comes before any of the text is the item's cover, not part of the story.
    first_text = next((p for p in body.find_all(["p", "li", "h2", "h3"]) if len(text_of(p)) > 40), None)
    lead = body.find("img")
    if lead and (first_text is None or lead in first_text.find_all_previous("img")) and \
            len(body.find_all("img")) <= 2:
        (lead.find_parent("figure") or lead).decompose()
    return body


def parse_page(html, url):
    """Everything the builders need from one page."""
    soup = BeautifulSoup(html, "html.parser")
    content = (SELECTORS["content"] and soup.select_one(SELECTORS["content"])) or main_content(soup)
    picked = SELECTORS["title"] and soup.select_one(SELECTORS["title"])
    title = text_of(picked) if picked else find_title(soup, content)
    og = soup.find("meta", property="og:image")
    # The content guess misses a gallery block on pages with little text, or takes "more galleries" with it.
    blocks = soup.select(GALLERY_BLOCKS)
    images = find_images(blocks if blocks else content, url)
    cover = None
    if og and clean_url(og.get("content"), url) and not IMAGE_SKIP.search(og["content"]):
        cover = {"url": clean_url(og["content"], url), "alt": ""}
    elif images:
        cover = dict(images[0])

    body = clean_body(copy.copy(content), title)
    for img in body.find_all("img"):
        img["src"] = image_url(img, url) or ""
    for a in body.find_all("a", href=True):
        a["href"] = clean_url(a["href"], url) or a["href"]
    meta_description = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", property="og:description")
    description = describe(content) or (meta_description.get("content", "").strip() if meta_description else "")
    page_text = text_of(content) or title
    page = {
        "url": url,
        "lang": language(title + " " + page_text[:2000]),
        "title": title,
        "date": find_date(soup, content),
        "description": description,
        "body": to_markdown(str(body)),
        "cover": cover,
        "images": images,
        "files": find_files(content, url),
        "videos": find_videos(content, url),
        "data": None,
        "other_lang": None,
        "facts": {},
        "soup": soup,
    }
    data = embedded_data(soup)
    if data is not None and len(page_text) < 300:
        use_embedded_data(page, data)
    if data is not None:
        use_record(page, main_record(data), clean_url(og and og.get("content"), url) or "")
    page["empty"] = len(page_text) < 40 and not page["images"] and not page["videos"] and not page["data"]
    return page


def embedded_data(soup):
    """The page's data when a JavaScript app renders it: Next.js page props, or JSON-LD."""
    script = soup.find("script", id="__NEXT_DATA__")
    try:
        if script and script.string:
            return json.loads(script.string).get("props", {}).get("pageProps") or None
        ld = [json.loads(s.string) for s in soup.find_all("script", type="application/ld+json") if s.string]
        return ld or None
    except ValueError:
        return None


def walk(value, key=""):
    """(key, value) for every string in nested JSON."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v, key)
    elif isinstance(value, str):
        yield key, value


def use_embedded_data(page, data):
    """Fill a JavaScript app's page from its embedded data; the keys are the usual CMS names."""
    strings = list(walk(data))
    pick = lambda names, thaana=False: next((v.strip() for name in names for k, v in strings
                                             if k.lower() == name and v.strip() and v.strip() != "-"
                                             and bool(THAANA.search(v)) == thaana), None)
    title_en = pick(["titleen", "title_en", "title", "name", "headline"])
    title_dv = pick(["title", "titledv", "title_dv", "name", "headline"], thaana=True)
    if title_en or title_dv:
        page["title"], page["lang"] = (title_en, "en") if title_en else (title_dv, "dv")
    page["description"] = pick(["descriptionen", "description_en", "description", "summary"]) or page["description"]
    date = next((parse_date(v) for k, v in strings if re.search(r"publish|^date", k, re.I) and parse_date(v)), None)
    page["date"] = page["date"] or date
    text = json.dumps(data, ensure_ascii=False)
    ids = re.findall(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/))([\w-]{11})", text)
    page["videos"] = page["videos"] or [f"https://www.youtube.com/watch?v={i}" for i in dict.fromkeys(ids)]
    page["data"] = text[:20000]
    if title_en and title_dv:
        description_dv = pick(["description", "descriptiondv", "description_dv"], thaana=True)
        page["other_lang"] = {**page, "lang": "dv", "title": title_dv, "description": description_dv or "",
                              "body": description_dv or "", "other_lang": None}


RICH_TAGS = {"paragraph": "p", "bulletList": "ul", "orderedList": "ol", "listItem": "li", "blockquote": "blockquote",
             "table": "table", "tableRow": "tr", "tableCell": "td", "tableHeader": "th"}
MARK_TAGS = {"bold": "strong", "italic": "em", "code": "code"}
# Record keys (lowercased) of the facts a project page lists, in the CMS names seen on gov.mv sites.
FACT_KEYS = {
    "status": ("status", "projectstatus"),
    "start": ("startdate", "start_date"),
    "end": ("enddate", "end_date", "expecteddate", "completiondate"),
    "location": ("location",),
    "funding": ("funding", "fundingorganisation", "donor"),
    "contact_name": ("contactname",),
    "contact_phone": ("contactnumber", "contactphone"),
    "contact_email": ("contactmail", "contactemail"),
}


def rich_html(node):
    """HTML for rich text stored as ProseMirror (Tiptap) JSON."""
    if isinstance(node, list):
        return "".join(rich_html(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        html = escape(node.get("text", ""))
        for mark in node.get("marks", []):
            tag = MARK_TAGS.get(mark.get("type"))
            if tag:
                html = f"<{tag}>{html}</{tag}>"
            elif mark.get("type") == "link":
                html = f'<a href="{escape((mark.get("attrs") or {}).get("href") or "")}">{html}</a>'
        return html
    if kind == "hardBreak":
        return "<br>"
    inner = rich_html(node.get("content", []))
    tag = f"h{(node.get('attrs') or {}).get('level', 3)}" if kind == "heading" else RICH_TAGS.get(kind)
    return f"<{tag}>{inner}</{tag}>" if tag else inner


def main_record(data):
    """The item's own record in a JavaScript app's page data, e.g. pageProps["news"]."""
    records = [v for v in data.values() if isinstance(v, dict)] if isinstance(data, dict) else []
    return max(records, key=len) if records else None


def use_record(page, record, share_image):
    """Facts, files and the full body from the item's record; the HTML shows only part of them."""
    if not record:
        return
    fields = {k.lower(): v for k, v in record.items()}
    value = lambda v: v.strip() if isinstance(v, str) and v.strip() not in ("", "-") else None
    facts = {name: next((value(fields.get(k)) for k in keys if value(fields.get(k))), None)
             for name, keys in FACT_KEYS.items()}
    category = fields.get("category")
    if isinstance(category, dict):
        facts["sector_en"] = value(category.get("nameEn")) or (None if THAANA.search(category.get("name") or "")
                                                              else value(category.get("name")))
        facts["sector_dv"] = value(category.get("nameDv")) or (value(category.get("name"))
                                                              if THAANA.search(category.get("name") or "") else None)
    page["facts"] = {k: v for k, v in facts.items() if v}
    page["date"] = page["date"] or next((parse_date(v) for k in ("publisheddate", "publishedat", "createdat")
                                         if isinstance(v := fields.get(k), str) and parse_date(v)), None)

    # Sections stored as rich text, most of which the page only shows after a click.
    sections = fields.get("details") if isinstance(fields.get("details"), list) else []
    html = "".join(f"<h3>{escape(value(d.get('title')) or '')}</h3>{rich_html(d.get('body'))}"
                   for d in sections if isinstance(d, dict))
    body = to_markdown(html) if html else ""
    if len(body) > len(page["body"]):
        page["body"] = body
        page["description"] = describe(BeautifulSoup(html, "html.parser")) or page["description"]

    # Files are stored by name; this CMS serves them next to its images (…/images/1500/x.webp -> …/images/x.pdf).
    base = re.match(r"(.*/images/)", share_image)
    entries = fields.get("files") if isinstance(fields.get("files"), list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = value(entry.get("href")) if entry.get("link") else None
        if not url and base and value(entry.get("file")):
            url = base.group(1) + quote(entry["file"])
        if url and url not in {f["url"] for f in page["files"]}:
            label = " ".join((value(entry.get("titleEn")) or value(entry.get("title")) or label_for(url)).split())
            page["files"].append({"label": label, "url": url})


# ---------------------------------------------------------------------------------------------- listings

def is_pagination(url):
    return bool(re.search(r"([?&](page|paged|p|start|offset)=\d+|/page/\d+/?$)", url))


def item_links(soup, url, pattern=None):
    """Links on a listing page that lead to its items, in page order."""
    scope = strip_boilerplate(soup)
    links = []
    for a in scope.find_all("a", href=True):
        link = clean_url(a["href"], url)
        if (not link or link.rstrip("/") == url.rstrip("/") or not same_site(link, url) or is_pagination(link)
                or FILE_EXT.search(urlparse(link).path) or re.search(r"\.(jpe?g|png|gif|webp)$", link, re.I)):
            continue
        links.append(link)
    links = list(dict.fromkeys(links))
    if pattern:
        return [link for link in links if re.search(pattern, link)]
    base_path = urlparse(url).path.rstrip("/") + "/"
    under = [link for link in links if urlparse(link).path.startswith(base_path)
             and urlparse(link).path.rstrip("/") != base_path.rstrip("/")]
    if len(under) >= 3:
        return under
    # Items are often elsewhere (/news -> /article/123): take the biggest group of sibling paths.
    groups = {}
    for link in links:
        path = urlparse(link).path.rstrip("/")
        parent, _, last = path.rpartition("/")
        # Posts can also sit at the site root under a number (WordPress: /45759).
        if (parent and (re.search(r"\d", last) or "-" in last or len(last) > 12)) or re.fullmatch(r"\d+", last):
            groups.setdefault(parent, []).append(link)
    best = max(groups.values(), key=len, default=[])
    return best if len(best) >= 3 else under


def next_page(soup, url, page_number):
    link = soup.find(["a", "link"], rel=lambda r: r and "next" in r, href=True)
    if link:
        return clean_url(link["href"], url)
    for a in soup.find_all("a", href=True):
        label = text_of(a) or a.get("aria-label", "") or a.get("title", "")
        if NEXT_TEXT.match(label) or re.search(r"\bnext\b", a.get("aria-label", "") + " " + " ".join(a.get("class") or []), re.I):
            candidate = clean_url(a["href"], url)
            if candidate and candidate != url:
                return candidate
    # Numbered pages: a link to page N+1.
    wanted = page_number + 1
    for a in soup.find_all("a", href=True):
        candidate = clean_url(a["href"], url) or ""
        if re.search(rf"([?&](page|paged|p)={wanted}(&|$)|/page/{wanted}/?$)", candidate):
            return candidate
    return None


def issue(url, problem, doc_id="", lang=""):
    """One line of the check list: something a person should verify by hand."""
    return {"id": doc_id, "language": lang, "problem": problem, "url": url}


def crawl_listing(session, url, args, first_html=None, issues=None):
    """Item URLs from a listing and the pages after it."""
    found, visited, page_number = [], set(), 1
    while url and url not in visited:
        visited.add(url)
        try:
            html = first_html if page_number == 1 and first_html else get(session, url, args.timeout)
        except RuntimeError as exc:
            if page_number == 1:
                raise
            html, reason = None, str(exc).replace(url, "").strip(": ")
        else:
            reason = "page not found"
        if not html:
            if page_number > 1 and issues is not None:
                issues.append(issue(url, f"listing page {page_number} not read ({reason}); "
                                         "items listed from there on were missed"))
            break
        soup = BeautifulSoup(html, "html.parser")
        new = [link for link in item_links(soup, url if page_number == 1 else args.url, args.item_pattern)
               if link not in found]
        if not new:
            break
        found.extend(new)
        log(f"  listing page {page_number}: {len(found)} items")
        if args.limit and len(found) >= args.limit:
            return found[:args.limit]
        url = next_page(soup, url, page_number)
        page_number += 1
        time.sleep(args.delay)
    return found


def inline_entries(page, fmt):
    """Items listed on the page itself, with no page of their own (a downloads list, a video wall)."""
    if fmt not in ("downloads", "video"):
        return []
    scope = strip_boilerplate(page["soup"])
    base = page["url"]
    # Filter tabs ("Forms", "Reports") name the type of each entry that repeats them.
    tabs = {text_of(a) for a in page["soup"].find_all("a", href=True) if "?" in a["href"] and text_of(a)}
    entries, seen = [], set()
    for el in scope.find_all(["a", "iframe", "video"]):
        url = clean_url(el.get("href") or el.get("src"), base)
        if not url or url in seen:
            continue
        if fmt == "downloads" and not FILE_EXT.search(urlparse(url).path):
            continue
        if fmt == "video":
            found = find_videos(BeautifulSoup(str(el), "html.parser"), base)
            if not found:
                continue
            url = found[0]
            if url in seen:
                continue
        inner = el.find(["h1", "h2", "h3", "h4", "h5", "strong"])
        title = text_of(inner) or el.get("title") or (text_of(el) if el.name == "a" else "")
        if not title and fmt == "downloads":
            continue  # an empty link left behind in the page's text
        seen.add(url)
        title = title or text_of(el.find_previous(["h2", "h3", "h4", "h5"]))
        kind = next((text_of(t) for t in el.find_all(True) if text_of(t) in tabs and text_of(t) != title), None)
        entry = {**page, "inline": True, "title": title or label_for(url), "category": kind, "description": "",
                 "body": "", "cover": None, "images": [], "videos": [], "files": [], "date": None}
        if fmt == "downloads":
            entry["files"] = [{"label": title or label_for(url), "url": url}]
        else:
            entry["videos"] = [url]
        entries.append(entry)
    return entries


# ---------------------------------------------------------------------------------------------- languages

def alternate_url(soup, url, lang):
    """The page's link to its version in the other language."""
    names = {"dv": ("ދިވެހި", "dhivehi", "divehi", "dv"), "en": ("english", "en", "އިނގިރޭސި")}[lang]
    for el in soup.find_all(["a", "link"], href=True):
        hreflang = (el.get("hreflang") or el.get("lang") or "").lower()
        if hreflang.startswith(lang) or text_of(el).lower() in names:
            candidate = clean_url(el["href"], url)
            # Many switchers only lead to the other language's home page; that is not this page.
            depth = lambda u: len([part for part in urlparse(u).path.split("/") if part and part not in ("en", "dv")])
            if candidate and candidate.rstrip("/") != url.rstrip("/") and depth(candidate) >= min(depth(url), 1):
                return candidate
    other = "en" if lang == "dv" else "dv"
    path = urlparse(url).path
    if re.search(rf"(^|/){other}(/|$)", path):
        return urlparse(url)._replace(path=re.sub(rf"(^|/){other}(/|$)", rf"\1{lang}\2", path, count=1)).geturl()
    return None


def fetch_alternate(session, page, args):
    want = "dv" if page["lang"] == "en" else "en"
    link = alternate_url(page["soup"], page["url"], want)
    if not link:
        return None
    time.sleep(args.delay)
    try:
        html = get(session, link, args.timeout)
    except RuntimeError:
        return None
    other = parse_page(html, link) if html else None
    return other if other and other["lang"] == want and other["title"] else None


def pair_key(page):
    """What one language's item shares with the other's: the page's slug, or for an entry listed on a page
    (a download, a video) its file or video, since every entry there has the listing's address."""
    if page.get("inline"):
        return (page["files"] and page["files"][0]["url"]) or (page["videos"] and page["videos"][0])
    return urlparse(page["url"]).path.rstrip("/").rsplit("/", 1)[-1]


def pair_by(en_pages, dv_pages):
    """Match Dhivehi pages to English ones by slug, else by date, else by position."""
    by_key = {}
    for dv in dv_pages:
        by_key.setdefault(pair_key(dv), dv)
    pairs = {i: by_key[pair_key(en)] for i, en in enumerate(en_pages) if pair_key(en) in by_key}
    # A site whose two languages share slugs has no pair for the rest: an English-only and a Dhivehi-only
    # item from the same day are different items. Entries on a listing have no date to go by.
    if pairs or any(page.get("inline") for page in en_pages + dv_pages):
        return pairs
    free = list(dv_pages)
    for i, en in enumerate(en_pages):
        same_day = [dv for dv in free if en["date"] and dv["date"] == en["date"]]
        if len(same_day) == 1:
            pairs[i] = same_day[0]
            free.remove(same_day[0])
    if not pairs and len(en_pages) == len(dv_pages):
        return dict(enumerate(dv_pages))
    return pairs


# ---------------------------------------------------------------------------------------------- main

def read_pages(session, url, args, single=False):
    """Parsed pages for every item under url (or url itself when it is one item)."""
    html = get(session, url, args.timeout)
    if not html:
        raise RuntimeError(f"{url} not found")
    first = parse_page(html, url)
    # Documents or videos listed on the page itself are the items; there is nothing to follow.
    inline = [] if single or args.single else inline_entries(first, args.format)
    if len(inline) >= 2:
        log(f"  {len(inline)} items listed on the page itself")
        return inline[:args.limit or None], []
    failed = []
    links = [] if single or args.single else crawl_listing(session, url, args, html, failed)
    if not links:
        missing = {"downloads": not first["files"], "video": not first["videos"],
                   "gallery": not first["images"]}.get(args.format, False)
        if first["empty"] or missing:
            wanted = {"downloads": "document links", "video": "videos", "gallery": "photos"}.get(args.format, "content")
            raise RuntimeError(f"found no item links or {wanted} on {url}. The page may load its content with "
                               "JavaScript (try the site's API, e.g. /wp-json/ on WordPress), or the links may need "
                               "--item-pattern")
        return [first], failed
    pages = []
    # What an item of this format cannot be without.
    needs = {"video": ("videos", "no video found"), "gallery": ("images", "no photos found"),
             "downloads": ("files", "no files found")}.get(args.format)
    for count, link in enumerate(links, 1):
        time.sleep(args.delay)
        reason = None
        try:
            item_html = get(session, link, args.timeout)
        except RuntimeError as exc:
            log(f"  error: {exc}")
            item_html, reason = None, f"could not load it: {str(exc).replace(link, '').strip(': ')}"
        page = parse_page(item_html, link) if item_html else None
        if not page:
            reason = reason or "page not found (404)"
        elif not page["title"]:
            reason = "no title found"
        elif page["empty"]:
            reason = "no content found (the page may load it with JavaScript)"
        elif needs and not page[needs[0]]:
            reason = needs[1]
        if reason:
            failed.append(issue(link, f"not scraped: {reason}"))
        else:
            pages.append(page)
        if count % 10 == 0 or count == len(links):
            log(f"  read {count}/{len(links)}")
    # A cover every item shares is the site's default share image, not the item's own.
    covers = [page["cover"]["url"] for page in pages if page["cover"]]
    for page in pages:
        if page["cover"] and covers.count(page["cover"]["url"]) > 1:
            page["cover"] = dict(page["images"][0]) if page["images"] else None
    return pages, failed


def write_output(docs, fmt, args):
    if args.stdout:
        json.dump(envelope(fmt, docs), sys.stdout, indent=2, ensure_ascii=False)
        print()
        return
    folder = args.output_dir or os.path.join("scraped", urlparse(args.url).hostname or "site", fmt)
    os.makedirs(folder, exist_ok=True)
    pages = max(1, -(-len(docs) // CHUNK_SIZE))
    for page in range(1, pages + 1):
        chunk = docs[(page - 1) * CHUNK_SIZE:page * CHUNK_SIZE]
        with open(os.path.join(folder, f"{fmt}-{page}.json"), "w", encoding="utf-8") as f:
            json.dump(envelope(fmt, chunk, total=len(docs)), f, indent=2, ensure_ascii=False)
    log(f"saved {len(docs)} docs to {folder}/ in {pages} file(s)")
    return folder


def report_issues(issues, fmt, folder=None):
    """Print the check list, and save it as CSV (opens in Excel, Thaana included) next to the JSON."""
    if not issues:
        log("nothing needs checking")
        return
    log(f"needs checking: {len(issues)}")
    for row in issues:
        log(f"  {row['id'] or '-'}  {('[' + row['language'] + '] ') if row['language'] else ''}"
            f"{row['problem']}  {row['url']}")
    if folder:
        path = os.path.join(folder, f"{fmt}-check.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "language", "problem", "url"])
            writer.writeheader()
            writer.writerows(issues)
        log(f"check list saved to {path}")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", help="listing page or single item page")
    parser.add_argument("-f", "--format", required=True, choices=list(FORMATS), help="output format")
    parser.add_argument("-o", "--output-dir", help="where to write files (default: scraped/<host>/<format>)")
    parser.add_argument("--stdout", action="store_true", help="print the JSON instead of writing files")
    parser.add_argument("-n", "--limit", type=int, default=0, help="first N items only (default: all)")
    parser.add_argument("--dv-url", help="Dhivehi listing to pair with the English one")
    parser.add_argument("--no-dv", action="store_true", help="do not look for Dhivehi versions")
    parser.add_argument("--single", action="store_true", help="treat the URL as one item, not a listing")
    parser.add_argument("--item-pattern", help="regex that item links on the listing must match")
    parser.add_argument("--title-selector", help="CSS selector for the title on item pages, when the rules miss it")
    parser.add_argument("--content-selector", help="CSS selector for the main content on item pages")
    parser.add_argument("--prefix", help="id prefix (default depends on the format, e.g. article, project)")
    parser.add_argument("--category", "--source-type", dest="category",
                        help="source_type / category / download type to use for every item")
    parser.add_argument("--no-llm", action="store_true", help="HTML rules only, no Claude")
    parser.add_argument("--model", default="claude-opus-5-5", help="Claude model (default: claude-opus-5-5)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1.0)")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    return parser


def scrape(args, api_key=None):
    """(docs, check list) for the run args describes: the check list names every page not scraped and every
    record missing a language or a field. Raises RuntimeError when nothing can be read."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    SELECTORS.update(title=args.title_selector, content=args.content_selector)
    claude = None if args.no_llm else Claude.connect(args.model, api_key)

    log(f"reading {args.url}")
    pages, failed = read_pages(session, args.url, args)
    others = {}
    if args.dv_url:
        log(f"reading {args.dv_url}")
        dv_pages, dv_failed = read_pages(session, args.dv_url, args)
        failed += dv_failed
        others = pair_by(pages, dv_pages)
        # Items only the second listing has (no version in the first language) are items too.
        paired = {id(page) for page in others.values()}
        pages += [page for page in dv_pages if id(page) not in paired]
    if not args.no_dv:
        # The language switcher finds the other version wherever the second listing has it, past --limit too.
        # Entries listed on one page share it; nothing to pair.
        urls = {page["url"] for page in pages} | {page["url"] for page in others.values()}
        for i, page in enumerate(pages):
            if i in others or page.get("inline"):
                continue
            other = fetch_alternate(session, page, args)
            if other and other["url"] not in urls:
                others[i] = other
    items, language_notes = [], []
    looked = bool(args.dv_url) or not args.no_dv
    names = {"en": "English", "dv": "Dhivehi"}
    for i, page in enumerate(pages):
        # A page from the Dhivehi listing is still English when the site picks the language with JavaScript;
        # its Dhivehi text is then in its embedded data.
        paired = others.get(i)
        candidates = [paired, paired and paired["other_lang"], page["other_lang"]]
        other = next((c for c in candidates if c and c["lang"] != page["lang"]), None)
        en, dv = (page, other) if page["lang"] == "en" else (other, page)
        items.append({"en": en, "dv": dv})
        missing = "dv" if page["lang"] == "en" else "en"
        if other or not looked or (page.get("inline") and not args.dv_url):
            language_notes.append(None)
        elif paired:
            language_notes.append((missing, f"the {names[missing]} page shows {names[page['lang']]} text "
                                            f"(no {names[missing]} translation): {paired['url']}"))
        else:
            language_notes.append((missing, f"no {names[missing]} version found"))
    if items and looked:
        both = sum(1 for item in items if item["en"] and item["dv"])
        log(f"  in both languages: {both}, in one language only: {len(items) - both}")
    docs = build_docs(args.format, items, args, claude)

    issues = list(failed)
    for item, doc, note in zip(items, docs, language_notes):
        main = item["en"] or item["dv"]
        if note:
            issues.append(issue(main["url"], note[1], doc["id"], note[0]))
        missing = missing_fields(args.format, doc)
        if missing:
            issues.append(issue(main["url"], "missing " + ", ".join(missing), doc["id"]))
        if item.get("claude_error"):
            issues.append(issue(main["url"], f"extra fields not filled: {item['claude_error']}", doc["id"]))
    return docs, issues


def main():
    args = build_parser().parse_args()
    try:
        docs, issues = scrape(args)
    except KeyboardInterrupt:
        log("\ninterrupted")
        return 1
    except (RuntimeError, requests.RequestException) as exc:
        log(f"error: {exc}")
        return 1

    folder = write_output(docs, args.format, args)
    report_issues(issues, args.format, folder)
    return 1 if any(row["problem"].startswith(("not scraped", "listing page")) for row in issues) else 0


if __name__ == "__main__":
    sys.exit(main())
