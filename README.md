# Site Scraper

Scrapes a government website into JSON for CMS import. Give it a listing page or a single item page, pick a
format, and it returns records in the shape of the matching file in [samples/](samples/):

| Format | For | Sample |
| --- | --- | --- |
| `publications` | News, press releases, articles | [publications-api-sample.json](samples/publications-api-sample.json) |
| `newsroom` | Same shape as publications | [newsroom-api-sample.json](samples/newsroom-api-sample.json) |
| `downloads` | Forms and documents | [downloads-api-sample.json](samples/downloads-api-sample.json) |
| `gallery` | Photo albums | [gallery-api-sample.json](samples/gallery-api-sample.json) |
| `projects` | Projects with sector, funding and timeline | [projects-api-sample.json](samples/projects-api-sample.json) |
| `video` | YouTube, Vimeo and other videos | [video-api-sample.json](samples/video-api-sample.json) |

Titles, dates, bodies (as Markdown), images, documents and videos are read from the HTML. Fields with no fixed
place in the HTML (project sector, funding and timeline, download types, categories) are filled in by Claude when
an Anthropic API key is available. Dhivehi versions are found through the site's language switcher.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

To use Claude, set your key first: `export ANTHROPIC_API_KEY="sk-ant-..."`. Without it, the HTML rules alone are
used.

## Web app

```bash
.venv/bin/python scraper_web.py
```

Opens http://127.0.0.1:8000: paste a link, pick a format, and preview, copy or download the JSON.
It only listens on your own computer.

## Command line

```bash
.venv/bin/python scrape_site.py https://education.gov.mv/en/news --format publications --limit 10
.venv/bin/python scrape_site.py https://education.gov.mv/dv/galleries --format gallery --stdout
```

Results go to `scraped/<site>/<format>-1.json` (100 records per file), or to the screen with `--stdout`.
Useful options (`--help` lists them all):

- `--limit N`: scrape only the first N items
- `--no-llm`: skip Claude
- `--model`: e.g. `claude-haiku-5-5` for cheaper big crawls
- `--dv-url`: a Dhivehi listing to pair with the English one
- `--category`: the same source type, category or download type for every item
- `--title-selector`, `--content-selector`, `--item-pattern`: help with sites whose layout the rules misread

Pages that load their content with JavaScript requests (not embedded in the HTML) can't be scraped; the tool
says so instead of producing empty records.
