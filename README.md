# Bib Agent

Bib Agent is a Google Scholar driven bibliography maintenance tool for personal BibTeX collections.

It is designed for the workflow where:

- you already have hand-curated `.bib` files,
- those manual entries must stay protected,
- new publications should be discovered from your Google Scholar profile,
- missing metadata should be enriched from stronger sources such as DOI/Crossref, publisher pages, and arXiv,
- repeated runs should be safe and idempotent,
- change notifications should be emailed when something new or updated is detected.

The project keeps a strict ownership boundary:

- manual BibTeX outside the managed block is preserved,
- agent-generated BibTeX lives only inside the marked managed block,
- one exception is supported: if a manual tech-report/preprint entry is later superseded by a journal or conference publication, the old manual tech-report entry may be removed and replaced by the new published entry.

## What It Does

Main capabilities:

- fetches publications from a Google Scholar profile in reverse publication-date order,
- supports authenticated headless Scholar access using a saved browser session,
- updates one or more BibTeX files,
- routes publications into logical buckets such as `conference`, `journal`, and `techreport`,
- enriches metadata from DOI/Crossref, publisher landing pages, and arXiv,
- emphasizes configured author names such as `Feiyi Wang`,
- generates stable BibTeX keys like `f7b-2026a`,
- writes text, JSON, and HTML change reports,
- emails a report when a run produces new or updated entries,
- can render a bibliography-only PDF to verify compilation.

## Repository Layout

Core files:

- [update_bibs.py](/Users/f7b/Bib-Agent/update_bibs.py)
- [config.json](/Users/f7b/Bib-Agent/config.json)
- [config.example.json](/Users/f7b/Bib-Agent/config.example.json)
- [DESIGN.md](/Users/f7b/Bib-Agent/DESIGN.md)
- [README.md](/Users/f7b/Bib-Agent/README.md)

Python package:

- [bib_agent/cli.py](/Users/f7b/Bib-Agent/bib_agent/cli.py)
- [bib_agent/config.py](/Users/f7b/Bib-Agent/bib_agent/config.py)
- [bib_agent/http.py](/Users/f7b/Bib-Agent/bib_agent/http.py)
- [bib_agent/scholar.py](/Users/f7b/Bib-Agent/bib_agent/scholar.py)
- [bib_agent/metadata.py](/Users/f7b/Bib-Agent/bib_agent/metadata.py)
- [bib_agent/bibtex.py](/Users/f7b/Bib-Agent/bib_agent/bibtex.py)
- [bib_agent/render.py](/Users/f7b/Bib-Agent/bib_agent/render.py)

Browser helper:

- [scripts/scholar_browser.mjs](/Users/f7b/Bib-Agent/scripts/scholar_browser.mjs)
- [package.json](/Users/f7b/Bib-Agent/package.json)
- [package-lock.json](/Users/f7b/Bib-Agent/package-lock.json)

Tests:

- [tests/test_bibtex.py](/Users/f7b/Bib-Agent/tests/test_bibtex.py)

Packaging:

- [pyproject.toml](/Users/f7b/Bib-Agent/pyproject.toml)

## Setup

### Requirements

- `python3` 3.11+
- `node` and `npm`
- Google Chrome
- a TeX installation if you want PDF render checks

### Install browser dependency

```bash
cd /Users/f7b/Bib-Agent
npm install
```

### Configure the project

Start from [config.example.json](/Users/f7b/Bib-Agent/config.example.json) and create your local [config.json](/Users/f7b/Bib-Agent/config.json).

Example:

```bash
cp config.example.json config.json
```

Then edit [config.json](/Users/f7b/Bib-Agent/config.json).

The most important sections are:

- `scholar`
  - Google Scholar profile id
- `bib_files`
  - which BibTeX files are enabled and where they live
- `routing`
  - where `conference`, `journal`, and `techreport` records should go
- `baseline`
  - current workflow uses manual bibs as baseline and only considers Scholar items after the configured cutoff year
- `author_emphasis`
  - names to emphasize and whether to preserve original name order
- `notifications`
  - email transport and recipients

### Scholar authentication bootstrap

If you want authenticated Scholar fetches:

```bash
python3 update_bibs.py auth-bootstrap
```

This saves Scholar browser session state to:

- [state/scholar_storage_state.json](/Users/f7b/Bib-Agent/state/scholar_storage_state.json)

### Gmail API email setup

This project is currently configured to use Gmail API notification delivery.

Important fields in [config.json](/Users/f7b/Bib-Agent/config.json):

```json
"notifications": {
  "enabled": true,
  "transport": "gmail_api",
  "gmail_sender": "fwang2@gmail.com",
  "gmail_token_file": "~/sys/gmail/gmail_token.json",
  "gmail_creds_file": "~/sys/gmail/gmail_credentials.json",
  "report_from": "fwang2@ornl.gov",
  "report_recipients": ["fwang2@ornl.gov"]
}
```

The token and credential files are local secrets and should not be committed.

## Usage

### Update bib files

```bash
python3 update_bibs.py update
```

This will:

- fetch Scholar records,
- reconcile them against manual and agent-owned BibTeX content,
- update the managed blocks,
- write reports,
- send an email only if the run has `new` or `updated` entries.

### Bootstrap baseline

```bash
python3 update_bibs.py bootstrap
```

### Render PDF compile check

```bash
python3 update_bibs.py render-pdf
```

Output PDF:

- [build/bibliography-check/bibliography_check.pdf](/Users/f7b/Bib-Agent/build/bibliography-check/bibliography_check.pdf)

## Reports

Each update writes:

- text report: [state/last_update_report.txt](/Users/f7b/Bib-Agent/state/last_update_report.txt)
- JSON report: [state/last_update_report.json](/Users/f7b/Bib-Agent/state/last_update_report.json)
- HTML report: [state/last_update_report.html](/Users/f7b/Bib-Agent/state/last_update_report.html)

The HTML report is also used as the preferred email body.

## GitHub Maintenance

### Files you should commit

These are the files that define and maintain the project itself:

- [README.md](/Users/f7b/Bib-Agent/README.md)
- [DESIGN.md](/Users/f7b/Bib-Agent/DESIGN.md)
- [update_bibs.py](/Users/f7b/Bib-Agent/update_bibs.py)
- [pyproject.toml](/Users/f7b/Bib-Agent/pyproject.toml)
- [package.json](/Users/f7b/Bib-Agent/package.json)
- [package-lock.json](/Users/f7b/Bib-Agent/package-lock.json)
- everything under [bib_agent](/Users/f7b/Bib-Agent/bib_agent)
- everything under [scripts](/Users/f7b/Bib-Agent/scripts)
- everything under [tests](/Users/f7b/Bib-Agent/tests)

Commit [config.example.json](/Users/f7b/Bib-Agent/config.example.json).

Commit [config.json](/Users/f7b/Bib-Agent/config.json) only if:

- you are comfortable publishing the current non-secret local paths and preferences, or
- you replace sensitive/local-only values with safe shared defaults first.

### Files you should usually not commit

These are machine-local, generated, or sensitive:

- [state/agent_state.json](/Users/f7b/Bib-Agent/state/agent_state.json)
- [state/last_update_report.txt](/Users/f7b/Bib-Agent/state/last_update_report.txt)
- [state/last_update_report.json](/Users/f7b/Bib-Agent/state/last_update_report.json)
- [state/last_update_report.html](/Users/f7b/Bib-Agent/state/last_update_report.html)
- [state/scholar_storage_state.json](/Users/f7b/Bib-Agent/state/scholar_storage_state.json)
- [node_modules](/Users/f7b/Bib-Agent/node_modules)
- [__pycache__](/Users/f7b/Bib-Agent/__pycache__)
- any local Gmail token/credential files
- any copied or local-only bib files used for testing

In your current setup, do not commit:

- `~/sys/gmail/gmail_token.json`
- `~/sys/gmail/gmail_credentials.json`

### Recommended GitHub-safe commit set

If you want a clean maintainable repository, the safest commit set is:

- source code
- tests
- `README.md`
- `DESIGN.md`
- dependency manifests
- optionally a sanitized `config.json`

and exclude:

- all `state/` outputs
- browser session state
- Gmail credentials/tokens
- `node_modules/`
- caches and build outputs

## Suggested Next Cleanup

Before pushing to GitHub, a good next step would be:

1. add a `.gitignore` for `state/`, `node_modules/`, `__pycache__/`, and secret files
2. keep `config.example.json` with safe placeholder values
3. keep your real local `config.json` out of version control if it contains personal paths or emails
