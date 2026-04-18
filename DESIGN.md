# Bibliography Agent Design

## Goals

This repository implements a Python bibliography management agent for one or more BibTeX files.

The default example configuration uses three logical publication buckets:

- `conference`
- `journal`
- `techreport`

However, these targets are optional and configurable. A different user may:

- disable one or more targets,
- route multiple publication types into the same BibTeX file,
- use different labels or file paths,
- keep only a single master BibTeX file.

The agent uses Google Scholar as the discovery source for publications under the configured profile and enriches missing metadata from more authoritative scholarly sources when possible.

## Non-Negotiable Requirements

1. Manual content is immutable.
   Existing hand-curated BibTeX outside the agent-managed region must never be changed by the agent.
2. Agent-owned content is clearly marked.
   Each managed file contains an explicit managed block and each generated entry is preceded by provenance metadata in a BibTeX comment.
3. The baseline is explicit.
   The manual bibliography files are treated as the baseline source of truth for pre-existing content, together with a configurable cutoff year.
4. Repeated runs are idempotent.
   The agent can be scheduled periodically and should update entries it created before, add newly discovered publications, and preserve manual content byte-for-byte.
5. Missing metadata should be enriched from authoritative sources when possible.
   The resolver prefers DOI-backed metadata and Crossref content negotiation. Publisher landing pages are queried opportunistically for citation metadata when reachable.
6. Feiyi Wang should be emphasized in author lists.
   Agent-generated author fields render configured target names in bold, preserving either `First Last` or `Last, First` formatting when configured.
7. Configuration lives in `config.json`.
8. Requirements and design are maintained in this file.
9. If an update run produces new or updated agent entries, a plain-text report can be emailed to a configured address.

## Ownership Model

Each managed BibTeX file is split into two ownership zones:

- Manual zone: everything outside the managed block. The agent never edits this content.
- Agent zone: the block between:
  - `% >>> BIB_AGENT_MANAGED_START >>>`
  - `% <<< BIB_AGENT_MANAGED_END <<<`

Within the agent zone, each entry is preceded by a single JSON-bearing comment:

```bibtex
% BIB_AGENT {"scholar_id":"1JMwC1sAAAAJ:xmdbLjM3F_sC","key":"yin2025ringx","category":"conference","source":"doi"}
@inproceedings{yin2025ringx,
  ...
}
```

That provenance comment allows later runs to:

- keep a stable BibTeX key,
- recognize agent-owned entries,
- refresh metadata safely,
- preserve agent entries that are no longer present on the live Scholar profile when configured to do so.

The managed file may live outside this repository. For example, the conference bibliography can point directly to `/Users/f7b/resume/2026/pub-conference.bib` via `config.json`.

The managed block is placed at the top of the file so newly generated entries appear before the historical manual bibliography.

## BibTeX Key Policy

Agent-generated keys follow the configured house style:

- prefix from `config.json`, for example `f7b` or `fwang2`
- separator from `config.json`, currently `-`
- publication year
- alphabetical suffix within the year

Examples:

- `f7b-2026a`
- `f7b-2026b`

When generating a new key, the agent scans all existing manual and agent-owned keys across the managed bibliographies and picks the next available suffix for that year. Existing agent keys are preserved on refresh.

## Bootstrap Flow

`python3 update_bibs.py bootstrap`

Bootstrap performs two actions:

1. Fetch the current reverse-chronological Google Scholar publication list for the configured profile.
2. Save all currently visible Scholar publication IDs into `state/agent_state.json` as `manual_snapshot_ids`.

This records the current baseline configuration and observed Scholar row count without touching hand-maintained BibTeX entries.

Bootstrap also ensures that the three BibTeX files exist and contain an empty managed block at the top of the file.

If a target BibTeX file already exists and contains manual content, bootstrap appends the managed block without rewriting the pre-existing entries.

## Authenticated Scholar Fetch

The updater can optionally use a saved authenticated browser session for Google Scholar.

- Session bootstrap command: `python3 update_bibs.py auth-bootstrap`
- Saved session file: `state/scholar_storage_state.json`
- Repeated update runs reuse that session in headless mode

The recommended workflow is:

1. Log into Google Scholar normally in the configured local Chrome profile.
2. Close Chrome.
3. Run `python3 update_bibs.py auth-bootstrap` once to capture Playwright storage state from that profile.
4. Run `python3 update_bibs.py update` normally; it will use the saved session headlessly when available.

This avoids repeated logins on every run, but the saved session may still expire and need to be refreshed occasionally.

## Update Flow

`python3 update_bibs.py update`

Update performs the following steps:

1. Read config and persistent state.
2. Fetch the current Scholar publication list.
3. Skip any publication whose Scholar year is at or before the configured cutoff year, unless the entry is already agent-owned.
4. Fetch the Scholar detail page for each candidate publication.
5. Enrich metadata using:
   - DOI content negotiation when a DOI is known,
   - Crossref title search when DOI is unknown,
   - publisher landing-page citation metadata when reachable.
6. Classify the publication into one of the logical publication types:
   - `conference`
   - `journal`
   - `techreport`
7. Route that logical publication type to a configured enabled BibTeX target.
8. Reconcile each post-cutoff candidate against existing bibliography content:
   - exact Scholar ID match against agent-owned entries,
   - DOI match against manual entries,
   - arXiv ID match against manual entries,
   - normalized title+year match against manual entries,
   - normalized title match with weaker confidence as a possible duplicate.
   - exception: if a manual tech-report/preprint entry is superseded by a journal or conference publication, remove the old manual tech-report entry and add the new published entry.
9. Rebuild each managed block from the desired agent-owned entries.
10. Validate the generated entries for balanced braces and duplicate keys before writing.
11. If configured and the run produced new or updated entries, email the text report to the configured recipient.

## PDF Compile Check

The repository includes a PDF render check to verify that the current bibliography files can be turned into a document successfully.

- Command: `python3 update_bibs.py render-pdf`
- Output: `build/bibliography-check/bibliography_check.pdf`

The render command copies the enabled configured `.bib` files into a temporary build directory, generates a small LaTeX document, and compiles it with `pdflatex` + `bibtex` using `bibunits` so each enabled bibliography target is rendered in its own section.

## Metadata Strategy

### Discovery

The reverse-ordered publication list comes from:

- `https://scholar.google.com/citations?hl=en&user=<PROFILE>&view_op=list_works&sortby=pubdate`

The implementation requests pages with `cstart` and `pagesize=100`.

### Scholar Parsing

For each publication, Scholar provides enough initial structure to seed enrichment:

- title
- abbreviated author list
- venue summary
- year
- detail-page link

The detail page often provides:

- full author list
- publication date
- journal or book title
- volume, issue, pages
- publisher landing-page link

### Authoritative Enrichment

The resolver prefers metadata in this order:

1. DOI content negotiation response (`application/vnd.citationstyles.csl+json`)
2. arXiv metadata when an arXiv identifier is present
3. Crossref search result with exact title match
4. Citation meta tags or JSON-LD from the publisher landing page
5. Scholar detail metadata
6. Scholar list metadata

This ordering intentionally prioritizes DOI-backed/publisher-backed metadata over Scholar summaries.

## Classification Rules

The agent first classifies each publication into a logical type:

- Conference:
  - Crossref type `proceedings-article`
  - Scholar detail has `Book`
  - venue text contains conference/workshop/symposium/proceedings
- Journal:
  - Crossref type `journal-article`
  - Scholar detail has `Journal`
  - volume or issue exists with a journal-like venue
- Tech report:
  - Crossref type `report`
  - Scholar or authoritative metadata indicates technical report, laboratory report, institution report, or arXiv preprint

If classification remains ambiguous, the updater falls back to the logical type `techreport` rather than guessing a journal or conference incorrectly.

That logical type is then routed through `config.json`:

```json
"routing": {
  "conference": "conference",
  "journal": "journal",
  "techreport": "techreport",
  "default": "techreport"
}
```

This means a user can organize bibliographies differently, for example:

- route all publication types to a single `all` target,
- disable `techreport` entirely,
- rename target keys while keeping the same logical publication classifier.

## Safety Guarantees

- Manual content is never rewritten.
- Exception: a manual tech-report/preprint entry may be removed if the agent detects that it has been superseded by a journal or conference publication.
- Existing agent entries are the only generated content eligible for refresh.
- BibTeX keys remain stable for agent entries after first creation.
- Empty or failed metadata enrichment does not cause a manual region rewrite.
- Writes happen only after validation succeeds.
- New Scholar records that already exist manually are skipped rather than imported again.

## Reconciliation Statuses

Each Scholar candidate is classified into one of these buckets during update:

- `old`: at or before the configured cutoff year and not agent-owned
- `new`: after the cutoff year and not matched to an existing bibliography entry
- `manual-existing`: matched to a manual bibliography entry by DOI, arXiv ID, or normalized title/year
- `agent-existing`: already owned by the agent by exact Scholar ID
- `updated`: agent-owned and regenerated content differs from the stored agent entry
- `unchanged-agent`: agent-owned and regenerated content is unchanged
- `possible-duplicate`: weak match to a manual entry by normalized title
- `superseded-manual-techreport`: manual tech-report/preprint matched and promoted to a new journal or conference entry

## Notifications

Email notifications are configured in `config.json` under `notifications`.

- Default transport is local `sendmail`
- Recipient address is configured by `notifications.email`
- Email is sent only when a run yields at least one `new` or `updated` entry
- The message body is the same plain-text report written to `state/last_update_report.txt`

Example:

```json
"notifications": {
  "enabled": true,
  "email": "fwang2@ornl.gov",
  "from_email": "fwang2@ornl.gov",
  "subject_prefix": "Bibliography Agent",
  "sendmail_path": "/usr/sbin/sendmail"
}
```

## Known Limitations

- Google Scholar HTML is unofficial and may change.
- Some publisher sites block scraping aggressively; in those cases DOI/Crossref metadata is used instead.
- This implementation does not attempt to rewrite or normalize manual BibTeX entries.
- Edge-case publication types are still routed through the three logical classifier buckets, but users can map those buckets into any enabled target layout they prefer.
