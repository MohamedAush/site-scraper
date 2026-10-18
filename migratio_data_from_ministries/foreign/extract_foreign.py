"""Extract news, statements and speeches from the foreign.gov.mv Joomla dump into newsroom JSON.

Reads foreigngov_Foreignmain-.sql next to this file and writes one folder per source type to
ready_to_import/foreign/ (news/, statements/, speeches/), 100 records per file, in the shape of
samples/newsroom-api-sample.json. English and Dhivehi versions of an article are paired through
Joomla's language associations, then by their "Ref:" number.

    .venv/bin/python migratio_data_from_ministries/foreign/extract_foreign.py
"""
import html
import json
import os
import re
import sys
from datetime import datetime
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, REPO)
from scrape_site import describe, label_for, to_markdown  # noqa: E402

DUMP = os.path.join(HERE, "foreigngov_Foreignmain-.sql")
OUT = os.path.join(REPO, "ready_to_import", "foreign")
PREFIX = "kx0s1_"
SITE = "https://www.foreign.gov.mv/"
RAW = "https://raw.githubusercontent.com/MohamedAush/site-scraper/main/ready_to_import/foreign/"
PER_FILE = 100

# Joomla category id -> (source type, output folder, language of the category)
CATEGORIES = {
    "18": ("News", "news", "en"),
    "28": ("News", "news", "dv"),
    "19": ("Statement", "statements", "en"),
    "29": ("Statement", "statements", "dv"),
    "41": ("Speech", "speeches", "en"),
    "50": ("Speech", "speeches", "dv"),
}
PUBLISHED = {"1", "2"}  # published and archived
FILE_TYPES = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rtf|odt)$", re.I)
REF = re.compile(r"^(.*?)[\s,–-]*\bRef\b\s*[:;.]?\s*(\S*\d\S*?)[;,.]?(?:\s*\([^)]*\))?\s*$", re.I)
MARKS = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")
THAANA = re.compile(r"[ހ-޿]")

ESCAPES = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "Z": "\x1a", "b": "\b"}
BARE = re.compile(r"[^,)]*")


def parse_values(sql, i):
    """Parse the `(…),(…);` tuples of a mysqldump INSERT starting at i."""
    rows = []
    while True:
        while sql[i] in " ,\r\n\t":
            i += 1
        if sql[i] == ";":
            return rows
        i += 1  # "("
        row = []
        while True:
            while sql[i] in " \n":
                i += 1
            if sql[i] == "'":
                i += 1
                parts = []
                while True:
                    quote_at, slash_at = sql.find("'", i), sql.find("\\", i)
                    if slash_at != -1 and slash_at < quote_at:
                        parts.append(sql[i:slash_at])
                        parts.append(ESCAPES.get(sql[slash_at + 1], sql[slash_at + 1]))
                        i = slash_at + 2
                    elif sql[quote_at + 1] == "'":
                        parts.append(sql[i:quote_at + 1])
                        i = quote_at + 2
                    else:
                        parts.append(sql[i:quote_at])
                        i = quote_at + 1
                        break
                row.append("".join(parts))
            else:
                m = BARE.match(sql, i)
                value = m.group().strip()
                row.append(None if value == "NULL" else value)
                i = m.end()
            while sql[i] in " \n":
                i += 1
            i += 1
            if sql[i - 1] == ")":
                break
        rows.append(row)


def table(sql, name):
    name = PREFIX + name
    create = re.search(r"CREATE TABLE `%s` \((.*?)\n\)" % name, sql, re.S).group(1)
    columns = re.findall(r"^\s+`([^`]+)`", create, re.M)
    rows = []
    for m in re.finditer(r"INSERT INTO `%s` \([^)]*\) VALUES\s*" % name, sql):
        rows += [dict(zip(columns, row)) for row in parse_values(sql, m.end())]
    return rows


def media_url(src):
    """Re-hosted copy in ready_to_import/foreign when we have it, else the old site's URL."""
    src = html.unescape(src.split("#")[0]).strip()
    if re.match(r"https?://(www\.)?foreign\.gov\.mv/", src):
        src = re.sub(r"https?://(www\.)?foreign\.gov\.mv/", "", src)
    if re.match(r"[a-z]+:", src, re.I) or src.startswith("//"):
        return src
    path = unquote(src).lstrip("/")
    if os.path.exists(os.path.join(OUT, path)):
        return RAW + quote(path)
    return SITE + quote(path)


def featured_image(row, soup):
    images = json.loads(row["images"] or "{}")
    for key in ("image_fulltext", "image_intro"):
        if images.get(key):
            url = media_url(images[key])
            alt = images.get(key + "_alt") or label_for(url)
            return {"url": url, "alt": alt}
    img = soup.find("img", src=True)
    if img:
        url = media_url(img["src"])
        return {"url": url, "alt": img.get("alt") or label_for(url)}
    return None


def attached_files(soup):
    files, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].split("?")[0]
        if not FILE_TYPES.search(href):
            continue
        url = media_url(a["href"])
        if url in seen:
            continue
        seen.add(url)
        text = " ".join(a.get_text(" ").split())
        label = text if text and not text.lower().startswith(("click", "here", "download")) else label_for(url)
        files.append({"label": label, "file": {"url": url}})
    return files


def read_article(row):
    raw = row["introtext"] + row["fulltext"]
    soup = BeautifulSoup(raw, "html.parser")
    # The Ref line becomes its own field. It is a paragraph of its own, a line before a <br>, or the end of a
    # dateline ("1 March 2017, Male' – Ref:2017-093"), and is sometimes repeated a few lines down.
    reference = None
    blocks = soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6"], limit=8)
    strings = [s for s in soup.find_all(string=True) if s.strip()][:20]
    for node in blocks + strings:
        if getattr(node, "decomposed", False) or node.parent is None:
            continue
        text = node.get_text(" ") if hasattr(node, "get_text") else str(node)
        m = REF.match(MARKS.sub("", text).replace("\xa0", " "))
        if not m or reference and m.group(2).lower() != reference.lower():
            continue
        reference = reference or m.group(2)
        prefix = m.group(1).strip(" ,–-")
        if prefix:
            node.replace_with(prefix) if isinstance(node, str) else setattr(node, "string", prefix)
        else:
            node.extract()
    title = " ".join(html.unescape(row["title"]).split())
    lang = CATEGORIES[row["catid"]][2]
    if row["language"] == "*" or lang == "en" and THAANA.search(title):
        lang = "dv" if THAANA.search(title) else "en"
    date = row["publish_up"] if row["publish_up"] and not row["publish_up"].startswith("0000") else row["created"]
    return {
        "id": row["id"],
        "lang": lang,
        "source_type": CATEGORIES[row["catid"]][0],
        "folder": CATEGORIES[row["catid"]][1],
        "published_at": date.replace(" ", "T") + "Z",
        "reference_number": reference,
        "featuredImage": featured_image(row, soup),
        "files": attached_files(soup),
        "text": {"title": title, "description": describe(soup) or row["metadesc"].strip(), "body": to_markdown(str(soup))},
    }


def pair(articles, associations):
    """Group each English article with its Dhivehi version: Joomla associations first, then Ref number."""
    by_id = {a["id"]: a for a in articles}
    partner = {}
    groups = {}
    for row in associations:
        if row["context"] == "com_content.item" and row["id"] in by_id:
            groups.setdefault(row["key"], []).append(by_id[row["id"]])
    for group in groups.values():
        en = [a for a in group if a["lang"] == "en"]
        dv = [a for a in group if a["lang"] == "dv"]
        if len(en) == 1 and len(dv) == 1 and en[0]["folder"] == dv[0]["folder"]:
            partner[en[0]["id"]], partner[dv[0]["id"]] = dv[0], en[0]

    def by_ref(lang):
        refs = {}
        for a in articles:
            if a["lang"] == lang and a["reference_number"] and a["id"] not in partner:
                refs.setdefault((a["folder"], a["reference_number"].lower()), []).append(a)
        return refs

    def days_apart(a, b):
        return abs((datetime.fromisoformat(a["published_at"][:19]) - datetime.fromisoformat(b["published_at"][:19])).days)

    dv_refs = by_ref("dv")
    for key, ens in by_ref("en").items():
        dvs = dv_refs.get(key, [])
        if len(ens) == 1 and len(dvs) == 1:
            partner[ens[0]["id"]], partner[dvs[0]["id"]] = dvs[0], ens[0]
        elif dvs and (len(ens) == 1 or len(dvs) == 1):
            # The old site reused a Ref number: take the version published closest in time.
            en, dv = min(((e, d) for e in ens for d in dvs), key=lambda pair: days_apart(*pair))
            if days_apart(en, dv) <= 3:
                partner[en["id"]], partner[dv["id"]] = dv, en

    docs = []
    for a in articles:
        other = partner.get(a["id"])
        if other and a["lang"] == "dv":
            continue  # written with its English version
        en, dv = (a, other) if a["lang"] == "en" else (None, a)
        main = en or dv
        doc = {"id": "article-" + main["id"], "source_type": main["source_type"], "published_at": main["published_at"]}
        reference = (en and en["reference_number"]) or (dv and dv["reference_number"])
        if reference:
            doc["reference_number"] = reference
        image = (en and en["featuredImage"]) or (dv and dv["featuredImage"])
        if image:
            doc["featuredImage"] = image
        files = (en["files"] if en else []) + (dv["files"] if dv else [])
        seen = set()
        files = [f for f in files if not (f["file"]["url"] in seen or seen.add(f["file"]["url"]))]
        if files:
            doc["files"] = files
        doc["translations"] = {}
        if en:
            doc["translations"]["en"] = en["text"]
        if dv:
            doc["translations"]["dv"] = dv["text"]
        docs.append((main["folder"], doc))
    return docs


def write(folder, docs):
    path = os.path.join(OUT, folder)
    os.makedirs(path, exist_ok=True)
    pages = (len(docs) + PER_FILE - 1) // PER_FILE
    for page in range(1, pages + 1):
        chunk = docs[(page - 1) * PER_FILE:page * PER_FILE]
        data = {"docs": chunk, "totalDocs": len(docs), "hasNextPage": page < pages,
                "nextPage": page + 1 if page < pages else None}
        with open(os.path.join(path, "%s_%03d.json" % (folder, page)), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return pages


def main():
    with open(DUMP, encoding="utf-8") as f:
        sql = f.read()
    rows = [r for r in table(sql, "content") if r["catid"] in CATEGORIES and r["state"] in PUBLISHED]
    articles = sorted((read_article(r) for r in rows), key=lambda a: a["published_at"], reverse=True)
    docs = pair(articles, table(sql, "associations"))
    for folder in ("news", "statements", "speeches"):
        mine = [d for f, d in docs if f == folder]
        both = sum(len(d["translations"]) == 2 for d in mine)
        refs = sum("reference_number" in d for d in mine)
        pages = write(folder, mine)
        print("%-10s %5d records (%d EN+DV, %d with Ref) in %d files" % (folder, len(mine), both, refs, pages))


if __name__ == "__main__":
    main()
