# PhDiscover

A PhD-position discovery engine for biomechanics and human-movement research,
built to run at zero cost from Iran.

The site is static HTML, the data is JSON, and the crawl runs on a local
Python + Playwright stack. No server, no database, no paid API.

## What actually works today

- 23 job boards registered; 22 return data
- ~200 raw postings harvested per run
- 4-gate filter (role, field, doctoral, topic) plus a second scoring pass
  after the detail page is fetched
- Static site with search, country/priority/fit filters, and a per-position
  evidence record
- Newsletter signup and delivery with provider failover

## Run it

```bash
# Full pipeline (~15-20 min; the browser stage is the slow part)
bash scripts/run_pipeline.sh

# Serve the site
cd web && python -m http.server 8765
# -> http://localhost:8765
```

Individual stages:

```bash
python scripts/probe_sources.py         # reachability of every source
python scripts/crawl_v2.py              # crawl every job board
python scripts/filter_and_merge.py      # 4 gates + fit scoring
python scripts/enrich_positions.py      # detail pages + second scoring pass
python scripts/publish_site.py          # write web/data/
python scripts/coverage_report.py       # regenerate data/COVERAGE.md
```

## Why there are so many moving parts

The naive version — one HTTP GET, parse the HTML — returned almost nothing.
Each of these was measured, not assumed:

| Problem | What actually happened | Fix |
|---|---|---|
| Cloudflare 403 | Plain HTTP blocked, a real browser cleared it | Playwright per source |
| TLS handshake failure | `researchjobs.com` cert does not match its hostname | `verify=False` / `ignore_https_errors` |
| HTTP 200, zero links | The list is rendered by JavaScript | Playwright, let JS hydrate |
| Wrong links extracted | Many boards are *not* job boards | Reclassified as funding schemes |
| Title is an institution | `canadianresearch.org` heads each card with the university | Title recovered from the teaser text |
| Title only in the URL | `applykite` puts badges in the anchor text | Read the slug |
| Teaser too short to judge | Client-rendered teasers carry no topic words | Hold the card, fetch the detail page, then judge |

## Source registry

`config/sources.yaml` holds all 56 sources with the status each one actually
earned:

- `is_board: true` — a real vacancy board, crawled
- `is_board: false` — a funding programme or institutional portal, not crawled
- `needs_browser: true` — must go through Playwright
- `verify_tls: false` — broken certificate upstream
- `status` — the measured outcome, with `notes` explaining anything unusual

`data/COVERAGE.md` is regenerated from that file plus the last crawl, and is the
honest report of what is and is not covered.

Note that 32 of the 56 registry entries are deliberately *not* crawled. The
original list mixed job boards with funding schemes (DFG, SNF, NWO, NSERC) and
institutional homepages. Those pages list calls and grants, not individual
vacancies, and will return zero forever. Counting them as failures would have
been misleading.

## Reliability

The Iranian network drops connections without warning. Two mechanisms absorb
that, and both are on by default:

**Retry with backoff** (`src/phdiscover/reliability.py`)

A failed request is retried once, with jittered backoff. Only *transient*
failures are retried — timeouts, resets, 502/503. A 403 from a bot wall is
never retried, because the answer will be identical and hammering the host
makes the block worse.

**Cache with merge-on-read** (`src/phdiscover/cache.py`)

Per-source results are stored in `data/cache/positions_by_source.json`. The
rule is asymmetric:

- a source that returned results → replaces its cached copy
- a source that failed → **keeps** its cached copy

So `applykite` returning 19 postings this run and timing out the next does not
delete those 19 positions. Cached rows are tagged `from_cache` with their age,
and anything older than 21 days is flagged stale in the report rather than
silently served.

Both are covered by `scripts/test_reliability.py` (22 checks, no network).

```bash
python scripts/test_reliability.py
```

## Scheduled crawling

`.github/workflows/crawl.yml` runs the whole pipeline daily at 03:17 UTC on
GitHub's runners — outside the Iranian network, where the unreachable boards
are reachable. It self-tests before crawling, refuses to publish zero
positions, and commits `web/data/` back to the repo.

Setup is in `scripts/setup_repo.sh`. Two settings have to be changed in the
GitHub UI because no script here can reach them:

- **Settings → Actions → Workflow permissions → Read and write** — the job
  commits data back
- **Settings → Pages → Deploy from a branch → main / (root)** — the site is
  static and needs no build step

## Deadline handling and the stale-page rule

Boards bury application deadlines in whatever format they please. `src/phdiscover/deadlines.py`
parses what the registry actually contains: ISO first (unambiguous), then a date next to a
deadline cue ("Deadline:", "apply by", "closes"), then any parseable date, since card text often
strips the label.

**Day vs month order** is the one place a wrong answer silently removes a live position, so it is
never guessed. `18/10` and `10/18` each have exactly one valid reading and are resolved. A truly
ambiguous `05/10` is parsed using the board's locale (`day_first`, day-first by default) and
flagged with lower confidence. A posting whose deadline cannot be read is **kept** — losing a live
opportunity costs the reader more than showing a closed one they can see is closed.

**Rolling posts** ("open until filled", "reviewed on a rolling basis") are never treated as expired.

Two consumers:

- `gate_deadline` in `scripts/filter_and_merge.py` drops expired postings before the topic gate, so
  a closed post neither reaches the site nor escapes through the thin-context fallback.
- The pagination loop in `scripts/crawl_v2.py` stops after `stale_page_run` **consecutive** pages in
  which *every* posting had a known past deadline. Boards sort newest-first, so a run like that
  means everything deeper is older still. A page of rolling posts, or one whose dates were
  unreadable, does **not** count toward the run — absence of evidence is not evidence of staleness,
  and stopping early would discard live positions.

## Pagination

`next_page_url` handles the four shapes in the wild (`?page=2`, `/page/2/`, `?offset=20`,
`/job/job/index?page=2`) without hardcoding any board, and rejects two traps found in real HTML: a
"Next" that points backwards, and a pager that jumps to a *different search mode* (`/jobs/phd` →
`/search/?…`), which silently drops the caller's filter.

Depth is per source: `max_pages` in `config/sources.yaml` (8 for the deep boards, 3 by default),
capped by the stale-page rule above.

## Live site

```
https://sqrfxss.github.io/phdiscover-engine/
```

The repository root is **not** the site — `index.html` lives in `web/`. GitHub Pages therefore
serves the `gh-pages` branch, whose root holds the deployed copy: `index.html`, `subscribe.html`
and `data/`. The daily crawl pushes there, not to `main`.

To publish by hand after a local crawl:

```bash
cd F:/hermes/phdiscover-engine
bash scripts/run_pipeline.sh
bash scripts/deploy_site.sh          # copies web/ to gh-pages and pushes
```

## Deploying the site

`scripts/deploy_site.sh` mirrors what the workflow does, so a local crawl and a scheduled one
produce the same result. It refuses to publish an empty `ranked_opportunities.json`.

Pages and the crawl both need write access, set once from the repository API:

```bash
gh api -X PUT repos/:owner/:repo/actions/permissions/workflow \
  -H 'Content-Type: application/json' \
  --input - <<< '{"default_workflow_permissions":"write"}'
```

Without it the crawl's push fails with `403 Permission denied for github-actions[bot]` after the
crawl has already succeeded — the work is lost, not the run.

## Environment

`pyproject.toml` declares the dependency ranges and stays authoritative for what the code may use.
`requirements.lock` pins the exact versions it was verified against.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock
.venv/Scripts/python.exe -m pip install -e . --no-deps
```

`--no-deps` on the second step matters: without it pip re-resolves the ranges in `pyproject.toml`
and can move a package past the version the lock records.

`run_pipeline.sh` picks the first interpreter that can import what the pipeline needs, so a stale
or partial `.venv` cannot silently shadow the system python — that produced
`ModuleNotFoundError: bs4` two steps into a run, after the probe had already rewritten the health
report. It also prints any drift from the lock, and the exact command that fixes it.

**Chromium**: Playwright's bundled build comes from `cdn.playwright.dev`, which returns
`403 "this service is not available in your location"` from Iran. `open_browser` therefore launches
the installed Chrome directly and falls back to the bundled build only where none is present (Linux
CI).

## Layout

```
config/sources.yaml           56 sources with measured status
scripts/crawl_v2.py           the crawler: HTTP + browser + pagination + extraction
scripts/filter_and_merge.py   5 gates: role, field, doctoral, deadline, topic
scripts/enrich_positions.py   detail pages, then a second scoring pass
scripts/publish_site.py       writes web/data/
scripts/coverage_report.py    regenerates the coverage report
scripts/test_reliability.py   22 checks on retry + cache, no network
scripts/test_pagination.py    17 checks on pager discovery, no network
scripts/test_deadlines.py     41 checks on date parsing and the stop rule
scripts/test_stale_stop.py    end-to-end crawl against a local fake board
scripts/check_published.py    17 checks on the published data
scripts/merge_crawls.py       unions two runs' postings by canonical URL
scripts/deploy_site.sh        publishes web/ to the gh-pages branch
requirements.lock             the versions the 100 checks passed on
src/phdiscover/reliability.py retry with jittered backoff
src/phdiscover/cache.py       per-source cache, merge-on-read
src/phdiscover/deadlines.py   deadline parsing + page freshness
web/index.html                the site (Persian, RTL)
web/subscribe.html            newsletter signup
src/phdiscover/email/         newsletter delivery with provider failover
data/                         crawl output, cache, coverage report
```

## Newsletter

`src/phdiscover/email/service.py` supports Resend (3,000/mo free) with Brevo
(300/mo) and SMTP as fallbacks. All three were reachable from this network; the
API key is the only thing missing.

```bash
cp .env.template .env         # add RESEND_API_KEY
python scripts/send_newsletter.py          # render a preview
python scripts/send_newsletter.py --send --to you@example.com
```

## Known limits

- `findaphd` and `findapostdoc` sit behind a bot wall that no browser
  fingerprint clears from this network. They need an account.
- `eluta` renders a 55 KB shell unless you have a Canadian Job Bank login.
- `phdjobs` and `nrc_canada` time out from here.
- Telegram's API is unreachable from this network, so the bot cannot be tested
  live. The website and newsletter path works.
- Nothing found via a search index rather than a direct fetch is presented as
  verified; those records are marked `search_snippet` with lower confidence.

## Author

**Saeid Soraghi** — M.Sc. Sports Biomechanics, Bu-Ali Sina University
ORCID: 0009-0002-6074-1022
