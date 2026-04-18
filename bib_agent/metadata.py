from __future__ import annotations

import html
import json
import re
import urllib.parse

from .http import HttpClient, safe_get_text


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _strip_latex_markup(value: str) -> str:
    cleaned = re.sub(r"\\textbf\s*\{([^}]*)\}", r"\1", value)
    cleaned = re.sub(r"\\text\s*\{([^}]*)\}", r"\1", cleaned)
    return cleaned.replace("{", "").replace("}", "").strip()


def extract_doi(*values: str | None) -> str | None:
    pattern = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", flags=re.I)
    for value in values:
        if not value:
            continue
        match = pattern.search(urllib.parse.unquote(value))
        if match:
            return match.group(1).rstrip(".,;)")
    return None


def extract_arxiv_id(*values: str | None) -> str | None:
    patterns = [
        re.compile(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", flags=re.I),
        re.compile(r"arXiv:([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", flags=re.I),
        re.compile(r"\b([0-9]{4}\.[0-9]{4,5})(?:v\d+)?\b"),
    ]
    for value in values:
        if not value:
            continue
        for pattern in patterns:
            match = pattern.search(value)
            if match:
                return match.group(1)
    return None


def _parse_meta_tags(page_html: str) -> dict:
    meta = {}
    for name, content in re.findall(
        r'<meta\s+(?:name|property)="([^"]+)"\s+content="([^"]*)"',
        page_html,
        flags=re.I,
    ):
        meta.setdefault(name.lower(), []).append(html.unescape(content))
    return meta


def _try_fetch_landing_metadata(client: HttpClient, url: str | None) -> dict:
    if not url:
        return {}
    page_html = safe_get_text(client, url)
    if not page_html:
        return {}

    meta = _parse_meta_tags(page_html)
    authors = meta.get("citation_author", [])
    return {
        "title": (meta.get("citation_title") or [None])[0],
        "authors": authors,
        "journal": (meta.get("citation_journal_title") or [None])[0],
        "booktitle": (meta.get("citation_conference_title") or [None])[0],
        "doi": (meta.get("citation_doi") or [None])[0],
        "volume": (meta.get("citation_volume") or [None])[0],
        "issue": (meta.get("citation_issue") or [None])[0],
        "pages": _combine_pages(
            (meta.get("citation_firstpage") or [None])[0],
            (meta.get("citation_lastpage") or [None])[0],
        ),
        "year": _extract_year((meta.get("citation_publication_date") or [None])[0]),
        "publisher": (meta.get("citation_publisher") or [None])[0],
        "url": (meta.get("citation_public_url") or [url])[0],
    }


def _extract_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d{4})", value)
    return int(match.group(1)) if match else None


def _combine_pages(first_page: str | None, last_page: str | None) -> str | None:
    if first_page and last_page:
        return f"{first_page}--{last_page}"
    return first_page or last_page


def _crossref_search(client: HttpClient, title: str, max_results: int) -> dict | None:
    query = urllib.parse.quote(title)
    url = f"https://api.crossref.org/works?query.title={query}&rows={max_results}"
    payload = client.get_json(url)
    items = payload.get("message", {}).get("items", [])
    target = normalize_title(title)
    for item in items:
        candidates = item.get("title") or []
        if candidates and normalize_title(candidates[0]) == target:
            return item
    return None


def _doi_csl(client: HttpClient, doi: str) -> dict | None:
    text = safe_get_text(
        client,
        f"https://doi.org/{urllib.parse.quote(doi, safe='/')}",
        headers={"Accept": "application/vnd.citationstyles.csl+json"},
    )
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _scholar_authors(detail_fields: dict, row: dict) -> list[str]:
    authors = detail_fields.get("authors")
    if authors:
        return [part.strip() for part in authors.split(",") if part.strip()]
    summary = row.get("authors_summary", "")
    return [part.strip() for part in summary.split(",") if part.strip()]


def _try_fetch_arxiv_metadata(client: HttpClient, arxiv_id: str | None) -> dict:
    if not arxiv_id:
        return {}
    metadata = _try_fetch_landing_metadata(client, f"https://arxiv.org/abs/{arxiv_id}")
    if metadata:
        metadata["arxiv_id"] = arxiv_id
        metadata["journal"] = metadata.get("journal") or f"arXiv preprint arXiv:{arxiv_id}"
        metadata["url"] = metadata.get("url") or f"https://arxiv.org/abs/{arxiv_id}"
    return metadata


def _choose_authors(*author_lists: list[str] | None) -> list[str]:
    best: list[str] = []
    for authors in author_lists:
        if not authors:
            continue
        if len(authors) > len(best):
            best = authors
    return best


def _merge(primary: dict, secondary: dict) -> dict:
    merged = dict(secondary)
    for key, value in primary.items():
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


def enrich_record(client: HttpClient, record: dict, max_search_results: int) -> dict:
    detail_fields = record.get("detail_fields", {})
    landing = _try_fetch_landing_metadata(client, record.get("publisher_url"))
    arxiv_id = extract_arxiv_id(
        record.get("publisher_url"),
        detail_fields.get("description"),
        detail_fields.get("journal"),
        detail_fields.get("book"),
        record.get("venue_summary"),
        record.get("title"),
    )
    arxiv = _try_fetch_arxiv_metadata(client, arxiv_id)
    doi = extract_doi(
        landing.get("doi"),
        record.get("publisher_url"),
        detail_fields.get("description"),
        detail_fields.get("journal"),
        detail_fields.get("book"),
    )

    crossref_item = None
    if doi:
        crossref_item = _doi_csl(client, doi)
    if not crossref_item:
        crossref_item = _crossref_search(client, record["title"], max_search_results)
        if crossref_item:
            doi = doi or crossref_item.get("DOI")

    scholar_year = (
        detail_fields.get("publication date") and _extract_year(detail_fields.get("publication date"))
    ) or record.get("year")

    scholar_seed = {
        "title": record["title"],
        "authors": _scholar_authors(detail_fields, record),
        "year": scholar_year,
        "scholar_year": scholar_year,
        "journal": detail_fields.get("journal"),
        "booktitle": detail_fields.get("book"),
        "volume": detail_fields.get("volume"),
        "issue": detail_fields.get("issue"),
        "pages": detail_fields.get("pages"),
        "publisher": detail_fields.get("publisher"),
        "institution": detail_fields.get("institution"),
        "venue_summary": record.get("venue_summary"),
        "publication_date": detail_fields.get("publication date"),
        "url": record.get("publisher_url"),
    }

    crossref_seed = {}
    if crossref_item:
        authors = []
        for author in crossref_item.get("author", []):
            given = author.get("given", "").strip()
            family = author.get("family", "").strip()
            full = " ".join(part for part in [given, family] if part)
            if full:
                authors.append(full)
        crossref_seed = {
            "title": _first(crossref_item.get("title")) or crossref_item.get("title"),
            "authors": authors,
            "year": _extract_year(json.dumps(crossref_item.get("issued", {}))) or _extract_year(
                json.dumps(crossref_item.get("published", {}))
            ),
            "publisher_year": _extract_year(json.dumps(crossref_item.get("issued", {}))) or _extract_year(
                json.dumps(crossref_item.get("published", {}))
            ),
            "journal": _first(crossref_item.get("container-title")),
            "booktitle": _first(crossref_item.get("container-title")),
            "volume": crossref_item.get("volume"),
            "issue": crossref_item.get("issue"),
            "pages": crossref_item.get("page"),
            "publisher": crossref_item.get("publisher"),
            "doi": crossref_item.get("DOI"),
            "url": crossref_item.get("URL"),
            "type": crossref_item.get("type"),
        }

    merged = _merge(crossref_seed, _merge(arxiv, _merge(landing, scholar_seed)))
    if scholar_year:
        merged["year"] = scholar_year
        merged["scholar_year"] = scholar_year
    merged["doi"] = doi or merged.get("doi")
    merged["arxiv_id"] = arxiv_id or merged.get("arxiv_id")
    merged["authors"] = _choose_authors(
        arxiv.get("authors"),
        landing.get("authors"),
        crossref_seed.get("authors"),
        scholar_seed.get("authors"),
    )
    if merged.get("arxiv_id") and not merged.get("doi"):
        merged["type"] = merged.get("type") or "posted-content"
    merged["category"] = classify_record(record, merged)
    return merged


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value


def classify_record(record: dict, merged: dict) -> str:
    record_text = " ".join(
        filter(
            None,
            [
                record.get("venue_summary"),
                merged.get("journal"),
                merged.get("booktitle"),
                merged.get("publisher"),
                merged.get("type"),
            ],
        )
    ).lower()

    if merged.get("arxiv_id") or "arxiv preprint" in record_text:
        return "techreport"

    if merged.get("type") == "journal-article" or merged.get("journal"):
        if "conference" not in record_text and "proceedings" not in record_text:
            return "journal"

    if merged.get("type") in {"proceedings-article", "proceedings"}:
        return "conference"

    if any(token in record_text for token in ["conference", "proceedings", "workshop", "symposium"]):
        return "conference"

    if merged.get("type") in {"report", "posted-content"}:
        return "techreport"

    if any(token in record_text for token in ["technical report", "tech report", "laboratory", "arxiv", "ornl"]):
        return "techreport"

    if merged.get("volume") or merged.get("issue"):
        return "journal"

    return "techreport"


def make_bib_key(
    year: int | None,
    existing_keys: set[str],
    key_config: dict,
    existing_key: str | None = None,
) -> str:
    if existing_key:
        return existing_key
    prefix = key_config.get("prefix", "f7b")
    separator = key_config.get("separator", "-")
    year_part = str(year or "xxxx")
    suffix_index = 0
    while True:
        suffix = _index_to_suffix(suffix_index)
        candidate = f"{prefix}{separator}{year_part}{suffix}"
        if candidate not in existing_keys:
            existing_keys.add(candidate)
            return candidate
        suffix_index += 1


def _index_to_suffix(index: int) -> str:
    letters = []
    current = index
    while True:
        current, remainder = divmod(current, 26)
        letters.append(chr(ord("a") + remainder))
        if current == 0:
            break
        current -= 1
    return "".join(reversed(letters))


def _person_name_variants(value: str) -> set[str]:
    cleaned = _strip_latex_markup(value)
    variants = set()
    normalized = normalize_title(cleaned)
    if normalized:
        variants.add(normalized)

    if "," in cleaned:
        last, first = [part.strip() for part in cleaned.split(",", 1)]
        reordered = " ".join(part for part in [first, last] if part)
        reordered_normalized = normalize_title(reordered)
        if reordered_normalized:
            variants.add(reordered_normalized)

    tokens = [token for token in re.split(r"[\s,]+", cleaned) if token]
    if len(tokens) >= 2:
        first = tokens[0].rstrip(".")
        last = tokens[-1].rstrip(".")
        variants.add(normalize_title(f"{first} {last}"))
        variants.add(normalize_title(f"{last}, {first}"))
        if first:
            variants.add(normalize_title(f"{first[0]} {last}"))
            variants.add(normalize_title(f"{last}, {first[0]}"))
    return {variant for variant in variants if variant}


def _render_emphasized_author(author: str, emphasis_config: dict) -> str:
    if emphasis_config.get("preserve_original_format", True):
        return rf"\textbf{{{_strip_latex_markup(author)}}}"
    return emphasis_config.get("render_as", r"\textbf{Feiyi Wang}")


def emphasize_authors(authors: list[str], emphasis_config: dict) -> str:
    targets = set()
    for name in emphasis_config.get("target_names", []):
        targets.update(_person_name_variants(name))
    formatted = []
    for author in authors:
        author_variants = _person_name_variants(author)
        if author_variants & targets:
            formatted.append(_render_emphasized_author(author, emphasis_config))
        else:
            formatted.append(author)
    return " and ".join(formatted)


def latex_escape(value: str) -> str:
    return (
        value.replace("\\", r"\\")
        .replace("&", r"\&")
        .replace("%", r"\%")
        .replace("_", r"\_")
        .replace("#", r"\#")
    )


def bibtex_entry(record: dict, key: str, emphasis_config: dict) -> tuple[str, dict]:
    category = record["category"]
    if category == "conference":
        entry_type = "inproceedings"
    elif category == "journal":
        entry_type = "article"
    else:
        entry_type = "techreport"

    fields: dict[str, str] = {
        "title": record["title"],
        "author": emphasize_authors(record.get("authors", []), emphasis_config),
        "year": str(record.get("year") or ""),
    }

    if entry_type == "inproceedings":
        fields["booktitle"] = record.get("booktitle") or record.get("journal") or record.get("venue_summary") or ""
    elif entry_type == "article":
        fields["journal"] = record.get("journal") or record.get("venue_summary") or ""
    else:
        fields["institution"] = record.get("institution") or record.get("publisher") or "Unknown institution"
        fields["type"] = "Technical Report"

    optional_map = {
        "volume": record.get("volume"),
        "number": record.get("issue"),
        "pages": record.get("pages"),
        "publisher": record.get("publisher"),
        "doi": record.get("doi"),
        "url": record.get("url"),
    }
    for field_name, field_value in optional_map.items():
        if field_value:
            fields[field_name] = str(field_value)

    ordered_fields = []
    for field_name in ["title", "author", "booktitle", "journal", "year", "volume", "number", "pages", "institution", "type", "publisher", "doi", "url"]:
        value = fields.get(field_name)
        if value:
            ordered_fields.append((field_name, value))

    lines = [f"@{entry_type}{{{key},"]
    for index, (field_name, value) in enumerate(ordered_fields):
        comma = "," if index < len(ordered_fields) - 1 else ""
        if field_name == "author":
            rendered = value
        else:
            rendered = latex_escape(value)
        lines.append(f"  {field_name} = {{{rendered}}}{comma}")
    lines.append("}")
    return "\n".join(lines), {"entry_type": entry_type, "fields": fields}
