#!/usr/bin/env python3
"""A local web page for scrape_site.py: paste a link, pick a format, get the JSON.

Run it, and it opens http://127.0.0.1:8000 in your browser. It only listens on this computer.
Set ANTHROPIC_API_KEY before starting it (or paste a key on the page) to let Claude fill the fields
the HTML rules cannot find.

  .venv/bin/python scraper_web.py [--port 8000] [--no-browser]
"""

import argparse
import json
import os
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import requests

from scrape_formats import FORMATS, envelope, set_log_handler
from scrape_site import build_parser, scrape

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
        self.failed = []
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
                docs, self.failed = scrape(self.args, self.api_key)
                self.result = envelope(self.args.format, docs)
                self.status = "done"
                self.log.append(f"done: {len(docs)} docs" + (f", {len(self.failed)} pages could not be read"
                                                             if self.failed else ""))
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

    def to_json(self):
        return {"id": self.id, "status": self.status, "log": self.log, "result": self.result,
                "failed": self.failed, "error": self.error, "format": self.args.format, "url": self.args.url,
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
