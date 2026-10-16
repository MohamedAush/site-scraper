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

Titles, dates, bodies (as Markdown), images, documents and videos are read from the HTML. On sites built as
JavaScript apps (Next.js), the page's embedded data is read too: project status, dates, location, funding,
contact, sector and documents, and body sections the page only shows behind tabs. Fields neither source has (such
as project timeline, funding source and download types) are filled in by Claude when an Anthropic API key is
available.

Dhivehi versions are found through the site's language switcher, in the page's embedded data, or by pairing with
a Dhivehi listing you give. Some sites have no Dhivehi version of a section; then only `en` is filled.

## Setup

Needs Python 3.9 or newer.

```bash
git clone https://github.com/MohamedAush/site-scraper.git
cd site-scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows, use `python` for `python3`, and `.venv\Scripts\pip` and `.venv\Scripts\python` for
`.venv/bin/pip` and `.venv/bin/python` here and below.

To use Claude, set your key first: `export ANTHROPIC_API_KEY="sk-ant-..."`. Without it, the HTML rules alone are
used.

## Web app

```bash
.venv/bin/python scraper_web.py
```

Opens http://127.0.0.1:8000: paste a link, pick a format, and preview, copy or download the JSON. While a
scrape runs, the Scrape button turns into a Stop button. You can paste an API key on the page for one scrape
instead of setting it in the terminal. The app only listens on your own computer; stop it with Ctrl+C, and
restart it after pulling changes so it runs the new code.

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

## Ready-to-import files

[ready_to_import/](ready_to_import/) holds finished JSON for the CMS, one folder per ministry site
(`environment`, `fisheries`, `health`, …). Check a scrape's output against its sample before adding it here, and
pull before you start (`git pull origin main`) so you don't redo a site someone else has done.

## Known limits

- Pages that load their content with separate JavaScript requests (not embedded in the HTML) can't be scraped;
  the tool says so instead of producing empty records.
- When every item on a site shares one share image (`og:image`), it is treated as the site's default and the
  item's first photo is used as the cover instead.
- Check a few records from each new site by hand: layouts vary, and `--title-selector`, `--content-selector` and
  `--item-pattern` fix most misreads.
