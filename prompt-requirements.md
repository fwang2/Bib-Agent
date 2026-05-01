# Prompt Requirements

The goal is that this document can serve as a high-level build prompt or implementation spec for reconstructing the Bibliography Agent from scratch.

## Project Goal

Build a Python-based bibliography management agent that:

- discovers publications from a Google Scholar profile,
- enriches incomplete metadata from authoritative sources,
- updates one or more BibTeX files safely,
- preserves manual curation by default,
- supports recurring unattended runs,
- produces human-readable reports,
- can verify renderability,
- can optionally notify by email and auto-commit bibliography changes.

The system should be able to operate as a practical personal bibliography agent, but should also be configurable enough to support other users with different file layouts, naming conventions, and delivery preferences.

## Core Functional Requirements

### 1. Manage BibTeX bibliographies, originally with three logical buckets

The initial target structure is:

- conference bibliography
- journal bibliography
- tech report / preprint bibliography

However, this must not be hard-coded as a mandatory structure. Another user may:

- have only one `.bib` file,
- have only two categories,
- route multiple logical publication types into the same target file,
- disable some targets entirely,
- use different labels and paths.

The agent should still classify publications logically as:

- `conference`
- `journal`
- `techreport`

but route them to output files via configuration.

### 2. All configuration should live in `config.json`

The system must use a `config.json` file for variable knobs and policy controls, such as:

- Scholar profile settings
- output bib file paths and enable/disable flags
- routing rules
- author emphasis rules
- baseline/cutoff settings
- report output paths
- notification settings
- authentication settings
- Git auto-commit settings

A Git-safe `config.example.json` template should exist alongside the real config.

### 3. Manual content must be preserved

The user’s manually curated old bibliography content must not be changed by the agent under normal operation.

This includes:

- not reformatting old entries,
- not rewriting fields,
- not reordering them,
- not modifying keys,
- not touching content outside the managed region.

### 4. Agent-owned content must be clearly marked

The agent must write only inside an explicit managed block in each configured bib file.

There should be:

- a start marker,
- an end marker,
- per-entry provenance metadata comment showing agent ownership.

The managed block should be inserted at the top of the file so new items appear first rather than being appended at the end.

### 5. Old baseline vs new content must be explicit

The system should distinguish existing/baseline content from future agent-managed content.

The initial idea was to use “everything visible on Scholar at the time of bootstrap” as the boundary, but that evolved into a stricter rule:

- baseline should be rebuilt from what is actually present in the old/manual bib files,
- update consideration should focus on items after the end of 2025,
- anything already present in manual bibs should not be duplicated into agent content.

So the effective intended rule is:

- treat manual bib files as the baseline source of truth,
- only consider post-2025 Scholar items as candidates for new automated management,
- skip items that already exist manually,
- unless they meet the special supersession exception described below.

### 6. Repeated runs must be idempotent and suitable for automation

The agent is intended to run periodically and automatically.

Therefore it must:

- update previously agent-created entries safely,
- merge in new changes when needed,
- avoid duplicate imports,
- avoid rewriting unchanged agent entries,
- preserve manual content,
- support repeated scheduled runs without accumulating garbage changes.

It should also maintain rolling state so it does not always rescan the full post-2025 candidate universe forever.

## Discovery and Fetch Requirements

### 7. Use Google Scholar as the discovery source

Discovery starts from the user’s Google Scholar profile, reverse ordered by publication date:

- `https://scholar.google.com/citations?hl=en&user=<PROFILE>&view_op=list_works&sortby=pubdate`

The agent must fetch enough profile rows and detail pages to detect candidate new/updated publications.

### 8. Authenticated Scholar access must be supported

Public anonymous Scholar scraping was not sufficient, because some profile content visible to the user was not visible to the public scraper.

Therefore the system must support:

- authenticated Scholar access,
- a one-time auth bootstrap flow,
- headless reuse of saved session state,
- no requirement to enter credentials into config,
- clear handling when the saved session expires.

The intended design is:

- one-time local browser/session bootstrap,
- saved Playwright/Chrome storage state,
- future headless runs reuse that state,
- repeated login should not be required every run,
- but occasional re-auth may still be necessary if Google expires the session.

### 9. It should work headlessly after bootstrap

The user specifically requires headless browser execution for recurring runs.

So the system must:

- support headless authenticated fetch after bootstrap,
- not depend on repeated interactive login,
- reuse saved session state,
- fail clearly when the session is missing or expired.

### 10. Browser setup must be portable across macOS and Linux

Hard-coded Chrome paths are not acceptable.

The system should:

- auto-detect Chrome/Chromium executable programmatically,
- auto-detect or infer the user-data directory when possible,
- support macOS and Linux by default,
- allow empty config fields for browser paths so runtime detection can fill them in,
- provide a precheck/autofill command to validate and optionally write safe defaults.

## Metadata and Reconciliation Requirements

### 11. Missing metadata should be filled from authoritative sources

Google Scholar is often incomplete or abbreviated, so the agent must enrich metadata from more authoritative sources when possible.

Preferred sources include:

- DOI-backed metadata
- publisher pages
- IEEE
- ACM
- scientific journal sites
- Crossref
- arXiv when relevant

### 12. arXiv should be used when Scholar is incomplete

If Scholar’s abbreviated author list is incomplete, but an arXiv page exists, the agent should use arXiv to recover:

- full author list,
- more complete title,
- other useful publication metadata.

### 13. Crossref matching should be conservative

If DOI is not known and Crossref search is used, the agent should avoid incorrect metadata adoption.

In practice, this means:

- prefer exact title matches,
- do not blindly take the first weakly related Crossref result.

### 14. The agent must distinguish new, old, duplicate, and updated entries

The user explicitly requested logic to classify records as:

- new
- old
- duplicate
- updated

That evolved into a richer reconciliation model, including:

- manual-existing
- agent-existing
- unchanged-agent
- possible-duplicate
- superseded-manual-techreport

The system should use multiple signals for reconciliation, not just Scholar ID.

### 15. Scholar ID alone is not sufficient

Scholar IDs are useful but not a durable universal publication identity.

The system should store and use multiple identity signals where available:

- `scholar_id`
- `doi`
- `arxiv_id`
- normalized title fingerprint

These should help:

- detect agent-owned entries,
- match against manual entries,
- refresh previously agent-created entries,
- reduce duplicate creation.

### 16. Reconciliation should use multiple match strategies

The intended match order is roughly:

1. exact Scholar ID for agent-owned entries
2. DOI match
3. arXiv ID match
4. normalized title + year match
5. weaker normalized title duplicate detection

This should allow the system to decide whether something is:

- already manual,
- already agent-owned,
- a possible duplicate,
- genuinely new,
- or updated relative to an existing agent entry.

### 17. Special supersession rule for manual tech reports / preprints

There is one explicit exception to “never touch manual entries”:

If a manual tech report or preprint entry has been superseded by a journal or conference publication, then:

- the new journal/conference entry should be added,
- the old manual tech report/preprint entry should be removed,
- this should be treated as a promotion/supersession workflow, not a duplicate.

This is the only stated exception to the normal immutability rule for manual content.

## Formatting and BibTeX Requirements

### 18. BibTeX keys should follow the user’s house style

New agent-generated keys should look like:

- `f7b-2026a`
- `f7b-2026b`

General rule:

- prefix from config, such as `f7b` or `fwang2`
- then year
- then alphabetical suffix within that year

The allocator should:

- scan existing keys across all relevant bib files,
- choose the next available suffix for that year,
- preserve existing agent keys on refresh.

### 19. Key year should follow Scholar’s visible year when needed

The year visible on the Scholar profile should control key generation and, in practice, should also be preferred for the rendered `year` field when enrichment metadata disagrees in a misleading way.

This was needed because authoritative sources may sometimes expose an earlier online/publication year that does not match the desired visible Scholar year.

### 20. The user’s name should be emphasized in the author field

The user wants their name emphasized in generated author lists.

Examples of acceptable forms include:

- `\textbf{F. Wang}`
- `\textbf{Feiyi Wang}`
- `\textbf{Wang, Feiyi}`

The system should make this configurable.

It should:

- default to enabled for this user,
- support multiple matching forms,
- recognize both `First Last` and `Last, First`,
- preserve original name format when configured,
- avoid malformed extra braces.

### 21. Agent metadata comments should be stable across machines

The agent writes `% BIB_AGENT {...}` metadata comments before generated entries.

Those comments should include stable provenance fields such as:

- `scholar_id`
- `doi`
- `arxiv_id`
- `key`
- `category`
- `source`
- `title_fingerprint`

But they should not persist volatile run-specific values like `status`, because that causes cross-machine churn and Git conflicts.

## Safety, Validation, and Render Requirements

### 22. The output should remain valid BibTeX / LaTeX-friendly content

The system should validate its generated content before writing.

It should guard against:

- duplicate keys,
- malformed braces,
- invalid managed block reconstruction.

### 23. There should be a PDF render/compile check

The user asked for a way to generate a PDF from the current bibliography content so they can verify the bibs compile and render correctly.

The system should therefore provide a command that:

- builds a temporary LaTeX document,
- includes all enabled bibliographies,
- compiles it,
- outputs a PDF,
- does not modify the source bibs during this check.

### 24. The system should surface when bib files contain unresolved merge conflicts

At minimum, the updater should not proceed blindly if a target bib file contains unresolved Git conflict markers, because that can corrupt further automated edits.

This is a safety guard, not the root solution to merge churn.

## Reporting and Notification Requirements

### 25. There should be a readable summary of each update run

The user wants an easy-to-read change summary suitable for email or review.

The system should produce reports that answer:

- whether anything changed,
- what was fetched,
- why no changes were made if none were made,
- which entries were new or updated,
- which files changed,
- which items were skipped because they already existed manually,
- which items were possible duplicates,
- which items were old baseline items.

### 26. Reports should exist in plain text and structured form

The system should write at least:

- a plain text report
- a JSON report

These reports should be saved on disk after each run.

### 27. There should be an HTML report suitable for email

The user wanted the output to be prettier and easier to digest.

So the system should also generate an HTML report that is:

- compact,
- visually readable,
- uses smaller fonts,
- uses color and muted styling,
- emphasizes changed files/items,
- shortens path clutter where possible.

This HTML should also be suitable as the body of an email notification.

### 28. Email notification should trigger only when there are meaningful changes

If a run results in:

- new entries, or
- updated entries

then a report should be emailed to a configured address.

If there are no new/updated entries, email should not be sent.

### 29. Email transport should be configurable and support Gmail API

Local `sendmail` was insufficient in practice.

Therefore the system should support a Gmail API-based notification transport using:

- sender Gmail account
- token file
- credentials file
- recipient list
- configurable report-from address

The email should preferably include the HTML report with a plain-text fallback.

### 30. There should be a test-email path

The user explicitly wanted a way to send a test email without waiting for a real change-triggering update.

So the system should support a test-send path for email verification.

## Operational Requirements

### 31. There should be a precheck/autofill command

Because environment setup differs across machines, especially macOS vs Ubuntu/Linux, the user wanted a precheck that can:

- validate required paths and tools,
- detect missing or invalid browser setup,
- detect mail-related paths,
- inspect bib path setup,
- optionally write safe detected fixes into config.

This should support a `--write` mode that applies safe autofixes.

### 32. Config paths should avoid hard-coded absolute machine-specific values

Paths in config and config example should prefer environment-based portability, such as `$HOME/...`, rather than hard-coded absolute home directories.

At the same time, browser executable detection should be programmatic rather than relying only on config paths.

### 33. The system should support optional Git auto-commit of changed bib files

If an update run makes actual changes, then the system should be able to:

- detect whether the destination bib directory is inside a Git repo,
- stage only the changed bib files,
- commit only those changed bib files,
- use a configurable commit message,
- make the whole feature optional and disable-able via config.

It should not accidentally commit unrelated work in the same repository.

### 34. The system should support scheduled execution

The user wanted Linux `crontab` use on a weekly basis.

So the system should be safe and predictable for unattended cron execution:

- use absolute paths in cron examples,
- run from the project directory,
- log stdout/stderr to a file,
- rely on internal email notification logic for meaningful changes.

## Documentation Requirements

### 35. Maintain `DESIGN.md`

The project should maintain a `DESIGN.md` file capturing the design and requirements.

### 36. Maintain `README.md`

The project should have a usable `README.md` that explains:

- what the project does,
- how to configure it,
- how to authenticate Scholar access,
- how to run updates,
- how to render PDF checks,
- how to set up notifications,
- what files should be committed to GitHub,
- what generated or secret files should not be committed.

### 37. Include a visual example of report/email output

The README should include a small rendered preview, such as a screenshot, so the email/report appearance is easy to understand quickly.

## Build-State Interpretation

If a system satisfies the requirements in this file, it should be in roughly the right state to “build the whole thing” that the user described.

In practical terms, that means:

- a Python CLI exists,
- config-driven Scholar discovery works,
- authenticated headless Scholar fetch is supported,
- enrichment from DOI/Crossref/publisher/arXiv is implemented,
- managed block writing is safe,
- reconciliation is multi-signal,
- post-2025/new-item policy is implemented,
- superseded manual preprints can be promoted,
- BibTeX keys and author emphasis follow the desired conventions,
- render verification exists,
- text/JSON/HTML reporting exists,
- email notifications exist,
- precheck exists,
- Git auto-commit exists as an option,
- documentation exists for setup and use.

## Suggested Acceptance Checklist

A fresh implementation should be considered close to complete if it can do all of the following:

- read a config file and locate enabled bib targets
- insert managed blocks at the top of those bib files without rewriting manual content
- authenticate once against Scholar and later fetch headlessly
- discover recent publications from the Scholar profile
- enrich metadata from DOI, publisher, Crossref, and arXiv
- detect whether a candidate is manual-existing, agent-existing, new, updated, duplicate, or superseded-manual-techreport
- generate stable house-style BibTeX keys
- bold the configured author correctly in multiple name formats
- write clean agent-managed entries with stable provenance comments
- validate before writing
- generate text, JSON, and HTML reports
- send an email when new/updated entries exist
- render a PDF check successfully
- optionally auto-commit changed bibs when enabled
- run safely under cron on Linux

