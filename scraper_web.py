#!/usr/bin/env python3
"""A local web page for scrape_site.py: paste a link, pick a format, get the JSON.

Run it, and it opens http://127.0.0.1:8000 in your browser. It only listens on this computer.
Set ANTHROPIC_API_KEY before starting it (or paste a key on the page) to let Claude fill the fields
the HTML rules cannot find.

  .venv/bin/python scraper_web.py [--port 8000] [--no-browser]
"""

import argparse
import csv
import io
import json
import os
import sys
import threading
import time
import traceback
import uuid
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

from scrape_formats import FORMATS, envelope, set_log_handler
from scrape_site import CHUNK_SIZE, build_parser, folder_name, scrape, scrape_split

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_web.html")
MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
JOBS = {}
# The scraper keeps its CSS selectors in module state, so scrapes run one at a time.
RUN_LOCK = threading.Lock()


class Cancelled(BaseException):
    """Raised inside a running scrape when its job is cancelled (BaseException so nothing swallows it)."""


class Job:
    def __init__(self, args, api_key):
        self.id = uuid.uuid4().hex[:12]
        self.args = args
        self.api_key = api_key
        self.status = "waiting"
        self.log = []
        self.result = None
        self.issues = []  # the check list: pages not scraped, records missing a language or a field
        self.error = None
        self.cancelled = False
        self.started = time.time()
        self.finished = None

    def stop_if_cancelled(self):
        if self.cancelled:
            raise Cancelled()

    def add(self, message):
        self.stop_if_cancelled()
        self.log.extend(line for line in str(message).splitlines() if line.strip())

    def run(self):
        set_log_handler(self.add, self.stop_if_cancelled)
        try:
            with RUN_LOCK:
                if self.cancelled:
                    raise Cancelled()
                self.status = "running"
                if self.args.split:
                    parts = scrape_split(self.args, self.api_key)
                    self.result = {"parts": [{"category": part["category"], "folder": folder_name(part["category"]),
                                              "result": envelope(self.args.format, part["docs"])} for part in parts]}
                    self.issues = [row for part in parts for row in part["issues"]]
                    count = f"{sum(len(part['docs']) for part in parts)} docs in {len(parts)} categories"
                else:
                    docs, self.issues = scrape(self.args, self.api_key)
                    self.result = envelope(self.args.format, docs)
                    count = f"{len(docs)} docs"
                self.status = "done"
                self.log.append(f"done: {count}" + (f", {len(self.issues)} to check" if self.issues
                                                    else ", nothing to check"))
        except Cancelled:
            self.status = "cancelled"
            self.log.append("cancelled")
        except (RuntimeError, requests.RequestException) as exc:
            self.status, self.error = "error", str(exc)
        except Exception as exc:  # a bug in the scraper: show it rather than leave the page waiting
            traceback.print_exc()
            self.status, self.error = "error", f"unexpected error: {exc!r}"
        finally:
            self.finished = time.time()
            set_log_handler(None)

    def zip_bytes(self):
        """Every JSON file (100 records each, one folder per category when split) and the check list."""
        fmt, buffer = self.args.format, io.BytesIO()
        parts = self.result.get("parts") or [{"folder": "", "result": self.result}]
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for part in parts:
                docs = part["result"]["docs"]
                for page in range(max(1, -(-len(docs) // CHUNK_SIZE))):
                    chunk = docs[page * CHUNK_SIZE:(page + 1) * CHUNK_SIZE]
                    name = os.path.join(part["folder"], f"{fmt}-{page + 1}.json")
                    archive.writestr(name, json.dumps(envelope(fmt, chunk, total=len(docs)), indent=2,
                                                      ensure_ascii=False))
            if self.issues:
                table = io.StringIO()
                writer = csv.DictWriter(table, fieldnames=["id", "language", "problem", "url"])
                writer.writeheader()
                writer.writerows(self.issues)
                archive.writestr(f"{fmt}-check.csv", "\ufeff" + table.getvalue())
        return buffer.getvalue()

    def to_json(self):
        return {"id": self.id, "status": self.status, "log": self.log, "result": self.result,
                "issues": self.issues, "error": self.error, "format": self.args.format, "url": self.args.url,
                "elapsed": round((self.finished or time.time()) - self.started, 1)}


def job_args(body):
    """argparse defaults for the scraper, overridden by the page's form."""
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Enter a full link starting with http:// or https://")
    if body.get("format") not in FORMATS:
        raise ValueError("Pick a format")
    args = build_parser().parse_args([url, "--format", body["format"]])
    args.stdout = True
    for name in ("dv_url", "item_pattern", "title_selector", "content_selector", "category", "prefix"):
        value = (body.get(name) or "").strip()
        setattr(args, name, value or None)
    args.limit = max(0, int(body.get("limit") or 0))
    args.delay = min(10.0, max(0.0, float(body.get("delay") if body.get("delay") not in (None, "") else 1.0)))
    args.no_llm = not body.get("use_claude", True)
    args.no_dv = not body.get("find_dv", True)
    args.single = bool(body.get("single"))
    args.split = bool(body.get("split")) and not args.single
    if body.get("model") in MODELS:
        args.model = body["model"]
    return args


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal for scrape progress

    def send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def local_only(self):
        # Refuse pages on other sites that point a hostname at this computer.
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send(403, {"error": "forbidden"})
            return False
        return True

    def do_GET(self):
        if not self.local_only():
            return
        path = urlparse(self.path).path
        if path == "/":
            with open(PAGE, "rb") as f:
                self.send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self.send(200, {"claude": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")),
                            "formats": list(FORMATS), "models": MODELS})
        elif path.startswith("/api/jobs/") and path.endswith("/zip") and path.split("/")[3] in JOBS:
            job = JOBS[path.split("/")[3]]
            if job.status != "done":
                self.send(409, {"error": "the scrape has not finished"})
                return
            host = (urlparse(job.args.url).hostname or "site").replace(".", "-")
            data = job.zip_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{host}-{job.args.format}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path.startswith("/api/jobs/") and path.split("/")[3] in JOBS:
            self.send(200, JOBS[path.split("/")[3]].to_json())
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.local_only():
            return
        path = urlparse(self.path).path
        if path == "/api/scrape":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                job = Job(job_args(body), (body.get("api_key") or "").strip() or None)
            except (ValueError, TypeError) as exc:
                self.send(400, {"error": str(exc)})
                return
            JOBS[job.id] = job
            threading.Thread(target=job.run, daemon=True).start()
            print(f"[{job.id}] {job.args.format}: {job.args.url}", file=sys.stderr)
            self.send(200, job.to_json())
        elif path.startswith("/api/jobs/") and path.endswith("/cancel") and path.split("/")[3] in JOBS:
            JOBS[path.split("/")[3]].cancelled = True
            self.send(200, {"ok": True})
        else:
            self.send(404, {"error": "not found"})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8000, help="port to listen on (default: 8000)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the page in a browser")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Scraper running at {url}  (Ctrl+C to stop)", file=sys.stderr)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
