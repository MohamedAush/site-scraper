"""Build the sample JSON formats from pages read by scrape_site.py, with Claude filling the fields
the HTML has no fixed place for.

Each item is {"en": page or None, "dv": page or None}. URLs (files, images, videos) always come from
the HTML; Claude only supplies text read from the pages, and null where the pages do not say.
"""

import os
import re
import sys
import threading
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel

# Format -> default id prefix and the top-level keys of its sample file.
FORMATS = {
    "publications": {"prefix": "article", "totals": True},
    "newsroom": {"prefix": "article", "totals": True},
    "downloads": {"prefix": "download", "totals": False},
    "gallery": {"prefix": "gallery", "totals": False},
    "projects": {"prefix": "project", "totals": False},
    "video": {"prefix": "video", "totals": False},
}
PAGE_TEXT_MAX = 60000  # characters of Markdown per language sent to Claude
_progress = threading.local()


def log(message):
    """Report progress: to stderr on the command line, or to the web app's job running in this thread."""
    handler = getattr(_progress, "handler", None)
    if handler:
        handler(message)
    else:
        print(message, file=sys.stderr)


def set_log_handler(handler, checkpoint=None):
    """Send this thread's progress to handler; checkpoint runs before each request and may raise to stop."""
    _progress.handler = handler
    _progress.checkpoint = checkpoint


def checkpoint():
    hook = getattr(_progress, "checkpoint", None)
    if hook:
        hook()


# ---------------------------------------------------------------------------------------------- Claude

class PublicationFields(BaseModel):
    source_type: Optional[str]
    published_date: Optional[str]
    description_en: Optional[str]
    description_dv: Optional[str]


class DownloadFields(BaseModel):
    type_name: Optional[str]
    description: Optional[str]
    file_labels: List[str]


class MediaFields(BaseModel):
    category: Optional[str]
    published_date: Optional[str]
    title_en: Optional[str]
    title_dv: Optional[str]


class ProjectText(BaseModel):
    title: Optional[str]
    description: Optional[str]
    sector: Optional[str]
    location: Optional[str]
    implementingOrganisation: Optional[str]
    fundingOrganisation: Optional[str]
    fundingSource: Optional[str]
    contact_name: Optional[str]


class TimelineEntry(BaseModel):
    date: Optional[str]
    type: Optional[str]
    completed: Optional[bool]
    title_en: Optional[str]
    description_en: Optional[str]
    title_dv: Optional[str]
    description_dv: Optional[str]


class ProjectFields(BaseModel):
    project_status: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    published_date: Optional[str]
    contact_phone: Optional[str]
    contact_email: Optional[str]
    en: ProjectText
    dv: Optional[ProjectText]
    timeline: List[TimelineEntry]


SYSTEM = """You extract structured fields from pages of a government website so they can be imported \
into a CMS. Use only facts stated in the page text you are given. When the text does not state a \
field, return null for it (or an empty list) - never guess, and never invent names, dates or numbers. \
Dates are YYYY-MM-DD. Keep Dhivehi text in Thaana script exactly as written, and fill Dhivehi fields \
only from the Dhivehi page."""

INSTRUCTIONS = {
    "publications": (PublicationFields, """\
source_type: what kind of item this is, as a short Title Case label such as News, Press Release, \
Announcement, Speech, Circular, Report, Publication, Statistics or Job - use the site's own wording \
when it names one.
published_date: the date the item was published.
description_en / description_dv: a one- or two-sentence summary taken from the item's own opening, \
in that language; null if that language's page is missing."""),
    "newsroom": None,  # same as publications
    "downloads": (DownloadFields, """\
type_name: the kind of document, as a short plural Title Case label such as Forms, Reports, \
Guidelines, Policies, Regulations or Templates.
description: one sentence on what the documents are for, from the page; null if it says nothing.
file_labels: one clean human-readable label per file, in the order the files are listed below."""),
    "gallery": (MediaFields, """\
category: the gallery's category (e.g. Events, Visits, Ceremonies) when the page names one.
published_date: the date of the gallery or event.
title_en / title_dv: the gallery title in each language, cleaned of site names and dates."""),
    "video": (MediaFields, """\
category: the video's category (e.g. Events, Speeches, Press Conferences) when the page names one.
published_date: the date the video was published.
title_en: the video's title, cleaned of site names. title_dv: the Dhivehi title if given."""),
    "projects": (ProjectFields, """\
project_status: one of planned, ongoing, completed, on-hold or cancelled, from what the page says.
start_date / end_date: the project's dates. published_date: when the page was published.
contact_phone / contact_email: the project's contact details.
en / dv: the project's details in each language (dv null if there is no Dhivehi page). \
description is a one- or two-sentence summary; sector, location, the implementing and funding \
organisations, funding source (e.g. Grant, Loan, Government budget) and contact name as written.
timeline: the project's milestones in date order. type is one of milestone, procurement, \
construction, signing, completion or other; completed is true for past milestones the page reports \
as done. Give the same entries' titles in both languages when the Dhivehi page lists them."""),
}


class Claude:
    def __init__(self, client, model):
        self.client = client
        self.model = model

    @classmethod
    def connect(cls, model, api_key=None):
        import anthropic
        if api_key:
            return cls(anthropic.Anthropic(api_key=api_key), model)
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            log("warning: ANTHROPIC_API_KEY is not set, so only HTML rules are used (fields such as "
                  "sector, funding, timeline and categories stay empty)")
            return None
        return cls(anthropic.Anthropic(), model)

    def extract(self, fmt, item, files=()):
        import anthropic
        model_class, instructions = INSTRUCTIONS[fmt] or INSTRUCTIONS["publications"]
        parts = [f"Fields to fill:\n{instructions}"]
        for lang, name in (("en", "English"), ("dv", "Dhivehi")):
            page = item[lang]
            if not page:
                continue
            body = page["body"]
            if len(body) > PAGE_TEXT_MAX:
                log(f"  note: {page['url']} is long; Claude sees its first {PAGE_TEXT_MAX} characters")
                body = body[:PAGE_TEXT_MAX]
            parts.append(f"<{name.lower()}_page url=\"{page['url']}\">\nTitle: {page['title']}\n"
                         f"Date found in HTML: {page['date'] or 'none'}\n\n{body}\n</{name.lower()}_page>")
        data = (item["en"] or {}).get("data") or (item["dv"] or {}).get("data")
        if data:
            parts.append(f"<page_data>\nThe page is rendered by JavaScript; this is the data it renders, which "
                         f"may hold both languages:\n{data}\n</page_data>")
        if files:
            parts.append("Files:\n" + "\n".join(f"{i + 1}. {f['label']} ({f['url']})" for i, f in enumerate(files)))
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": "\n\n".join(parts)}],
                output_format=model_class,
                # Re-run on another model if this one declines.
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )
        except anthropic.AuthenticationError:
            log("error: Claude rejected the API key; continuing with HTML rules only")
            self.client = None
            return None
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            log(f"  Claude failed for {(item['en'] or item['dv'])['url']}: {exc}")
            return None
        if response.stop_reason in ("refusal", "max_tokens") or response.parsed_output is None:
            log(f"  Claude returned no fields ({response.stop_reason}) for "
                  f"{(item['en'] or item['dv'])['url']}")
            return None
        return response.parsed_output


# ---------------------------------------------------------------------------------------------- helpers

def prune(value):
    """Drop empty values, as the samples leave out fields they do not have."""
    if isinstance(value, dict):
        value = {k: prune(v) for k, v in value.items()}
        return {k: v for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [v for v in (prune(v) for v in value) if v not in (None, "", [], {})]
    return value


def timestamp(date):
    return f"{date}T00:00:00Z" if date and re.match(r"^\d{4}-\d{2}-\d{2}$", date) else None


def valid_date(date):
    return date if date and re.match(r"^\d{4}-\d{2}-\d{2}$", date) else None


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def first(*values):
    return next((v for v in values if v), None)


def make_id(prefix, page, index, used):
    last = urlparse(page["url"]).path.rstrip("/").rsplit("/", 1)[-1] if page else ""
    number = re.findall(r"\d+", last)
    key = number[-1] if number and not page.get("inline") else (slugify(last) if last and not page.get("inline") else "")
    key = key or str(index)
    doc_id = f"{prefix}-{key}"
    n = 2
    while doc_id in used:
        doc_id = f"{prefix}-{key}-{n}"
        n += 1
    used.add(doc_id)
    return doc_id


def image(img, alt):
    return {"url": img["url"], "alt": img.get("alt") or alt} if img else None


def file_entries(files, labels=None):
    labels = list(labels or [])
    return [{"label": (labels[i] if i < len(labels) and labels[i] else f["label"]), "file": {"url": f["url"]}}
            for i, f in enumerate(files)]


def merged_files(item):
    """English files first, then any only the Dhivehi page has."""
    files, seen = [], set()
    for lang in ("en", "dv"):
        for f in (item[lang] or {}).get("files", []):
            if f["url"] not in seen:
                seen.add(f["url"])
                files.append(f)
    return files


# ---------------------------------------------------------------------------------------------- builders

def build_publication(item, fields, args):
    en, dv, main = item["en"], item["dv"], item["en"] or item["dv"]
    translations = {}
    for lang, page in (("en", en), ("dv", dv)):
        if page:
            summary = getattr(fields, f"description_{lang}", None) if fields else None
            translations[lang] = {"title": page["title"], "description": summary or page["description"],
                                  "body": page["body"]}
    title = first(en and en["title"], dv and dv["title"])
    return {
        "source_type": first(args.category, fields and fields.source_type, "News"),
        "published_at": timestamp(first(main["date"], fields and valid_date(fields.published_date))),
        "featuredImage": image(main["cover"], title),
        "files": file_entries(merged_files(item)),
        "translations": translations,
    }


def build_download(item, fields, args):
    main = item["en"] or item["dv"]
    type_name = first(args.category, fields and fields.type_name, main.get("category"), "Documents")
    return {
        "title": main["title"],
        "description": first(fields and fields.description, main["description"]),
        "type": {"name": type_name, "slug": slugify(type_name)},
        "files": file_entries(merged_files(item), fields and fields.file_labels),
    }


def build_gallery(item, fields, args):
    en, dv, main = item["en"], item["dv"], item["en"] or item["dv"]
    title_en = first(fields and fields.title_en, en and en["title"])
    title_dv = first(fields and fields.title_dv, dv and dv["title"])
    title = first(title_en, title_dv)
    photos = [image(p, title) for p in main["images"]]
    return {
        "category": {"name": first(args.category, fields and fields.category)},
        "publishedAt": timestamp(first(main["date"], fields and valid_date(fields.published_date))),
        "coverPhoto": image(main["cover"] or (main["images"] or [None])[0], title),
        "photos": photos,
        "translations": {"en": {"title": title_en} if en or title_en else None,
                         "dv": {"title": title_dv} if title_dv else None},
    }


def build_video(item, fields, args):
    main = item["en"] or item["dv"]
    video = (main["videos"] or [None])[0]
    title = first(fields and fields.title_en, main["title"], fields and fields.title_dv)
    hosted = video and re.search(r"(youtube\.com|youtu\.be)", video)
    return {
        "title": title,
        "category": {"name": first(args.category, fields and fields.category)},
        "publishedAt": timestamp(first(main["date"], fields and valid_date(fields.published_date))),
        "videoUrl": video,
        # YouTube supplies its own thumbnail; videos hosted elsewhere bring a cover.
        "coverPhoto": None if hosted else image(main["cover"], title),
    }


def build_project(item, fields, args):
    en, dv, main = item["en"], item["dv"], item["en"] or item["dv"]
    timeline = list(fields.timeline) if fields else []
    translations = {}
    for lang, page in (("en", en), ("dv", dv)):
        text = getattr(fields, lang, None) if fields else None
        if not page and not text:
            continue
        translations[lang] = {
            "title": first(text and text.title, page and page["title"]),
            "description": first(text and text.description, page and page["description"]),
            "body": page and page["body"],
            "sector": text and text.sector,
            "location": text and text.location,
            "implementingOrganisation": text and text.implementingOrganisation,
            "fundingOrganisation": text and text.fundingOrganisation,
            "fundingSource": text and text.fundingSource,
            "contact": {"name": text and text.contact_name},
            "timeline": [{"title": getattr(t, f"title_{lang}"), "description": getattr(t, f"description_{lang}")}
                         for t in timeline],
        }
    title = first(en and en["title"], dv and dv["title"])
    return {
        "projectStatus": fields and fields.project_status,
        "startDate": fields and valid_date(fields.start_date),
        "endDate": fields and valid_date(fields.end_date),
        "publishedAt": timestamp(first(main["date"], fields and valid_date(fields.published_date))),
        "coverPhoto": image(main["cover"], title),
        "contact": {"phone": fields and fields.contact_phone, "email": fields and fields.contact_email},
        "timeline": [{"date": valid_date(t.date), "type": t.type, "completed": t.completed} for t in timeline],
        "resources": file_entries(merged_files(item)),
        "translations": translations,
    }


BUILDERS = {"publications": build_publication, "newsroom": build_publication, "downloads": build_download,
            "gallery": build_gallery, "projects": build_project, "video": build_video}


def build_docs(fmt, items, args, claude):
    prefix = args.prefix or FORMATS[fmt]["prefix"]
    docs, used = [], set()
    for index, item in enumerate(items, 1):
        checkpoint()
        fields = None
        # A category given on the command line is all Claude would add for these formats.
        skip = fmt in ("gallery", "video") and args.category and (item["en"] or item["dv"])["date"]
        if claude and claude.client and not skip:
            fields = claude.extract(fmt, item, merged_files(item) if fmt == "downloads" else ())
        doc = {"id": make_id(prefix, item["en"] or item["dv"], index, used)}
        doc.update(BUILDERS[fmt](item, fields, args))
        docs.append(prune(doc))
        if claude and claude.client and (index % 10 == 0 or index == len(items)):
            log(f"  extracted {index}/{len(items)}")
    return docs


def envelope(fmt, docs, total=None):
    if FORMATS[fmt]["totals"]:
        return {"docs": docs, "totalDocs": total if total is not None else len(docs), "hasNextPage": False,
                "nextPage": None}
    return {"docs": docs, "hasNextPage": False}
