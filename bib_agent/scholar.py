from __future__ import annotations

import html
import json
import re
import subprocess
import urllib.parse

from .config import detect_chrome_executable, resolve_path
from .http import HttpClient, url_with_query


LIST_URL = "https://scholar.google.com/citations"


def _strip_tags(text: str) -> str:
    clean = re.sub(r"<[^>]+>", "", text)
    return html.unescape(clean).replace("\xa0", " ").strip()


def _extract_rows(page_html: str) -> list[dict]:
    rows = []
    for row_html in re.findall(r'<tr class="gsc_a_tr">(.*?)</tr>', page_html, flags=re.S):
        title_match = re.search(
            r'<a href="([^"]+citation_for_view=[^"]+)" class="gsc_a_at">(.*?)</a>',
            row_html,
            flags=re.S,
        )
        if not title_match:
            continue

        gray_blocks = re.findall(r'<div class="gs_gray">(.*?)</div>', row_html, flags=re.S)
        year_match = re.search(r'<span class="gsc_a_h[^"]*">\s*(\d{4})\s*</span>', row_html)
        href = html.unescape(title_match.group(1))
        scholar_id = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get(
            "citation_for_view", [None]
        )[0]
        if not scholar_id:
            continue

        rows.append(
            {
                "scholar_id": scholar_id,
                "title": _strip_tags(title_match.group(2)),
                "authors_summary": _strip_tags(gray_blocks[0]) if gray_blocks else "",
                "venue_summary": _strip_tags(gray_blocks[1]) if len(gray_blocks) > 1 else "",
                "year": int(year_match.group(1)) if year_match else None,
                "detail_url": urllib.parse.urljoin("https://scholar.google.com", href),
            }
        )
    return rows


def fetch_profile_page(client: HttpClient, config: dict, start: int) -> list[dict]:
    scholar_config = config["scholar"]
    profile_id = scholar_config["profile_id"]
    language = scholar_config.get("language", "en")
    page_size = int(scholar_config.get("page_size", 100))
    sort_by = scholar_config.get("sort_by", "pubdate")
    page_url = url_with_query(
        LIST_URL,
        hl=language,
        user=profile_id,
        view_op="list_works",
        sortby=sort_by,
        cstart=start,
        pagesize=page_size,
    )
    return _extract_rows(_get_page_html(client, page_url, config))


def _browser_fetch_html(url: str, config: dict) -> str:
    auth_config = config.get("auth", {})
    browser_script = resolve_path(config, auth_config["browser_script"])
    storage_state = resolve_path(config, auth_config["storage_state_path"])
    chrome_executable = auth_config.get("chrome_executable")
    chrome_path = None
    if chrome_executable:
        candidate = resolve_path(config, chrome_executable)
        if candidate.exists():
            chrome_path = candidate
    if chrome_path is None:
        chrome_path = detect_chrome_executable()
    if chrome_path is None:
        raise RuntimeError(
            "Could not locate a Chrome/Chromium executable automatically. "
            "Set auth.chrome_executable in config.json or run precheck --write."
        )
    command = [
        "node",
        str(browser_script),
        "fetch-url",
        "--url",
        url,
        "--storage-state",
        str(storage_state),
        "--chrome-executable",
        str(chrome_path),
        "--headless",
        "true" if auth_config.get("headless", True) else "false",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Authenticated Scholar fetch failed. "
            f"Command: {' '.join(command)}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    payload = json.loads(completed.stdout)
    return payload["html"]


def _get_page_html(client: HttpClient, url: str, config: dict) -> str:
    auth_config = config.get("auth", {})
    storage_state = resolve_path(config, auth_config.get("storage_state_path", "state/scholar_storage_state.json"))
    if auth_config.get("enabled"):
        if storage_state.exists():
            return _browser_fetch_html(url, config)
        if auth_config.get("require_session"):
            raise RuntimeError(
                f"Scholar authenticated session is required but missing: {storage_state}. "
                "Run `python3 update_bibs.py auth-bootstrap` first."
            )
    return client.get_text(url)


def scholar_fetch_mode(config: dict) -> str:
    auth_config = config.get("auth", {})
    storage_state = resolve_path(config, auth_config.get("storage_state_path", "state/scholar_storage_state.json"))
    if auth_config.get("enabled") and storage_state.exists():
        return "authenticated-headless"
    return "public-http"


def fetch_profile_rows(client: HttpClient, config: dict) -> list[dict]:
    scholar_config = config["scholar"]
    page_size = int(scholar_config.get("page_size", 100))
    max_items = int(scholar_config.get("max_items", 300))

    rows: list[dict] = []
    for start in range(0, max_items, page_size):
        page_rows = fetch_profile_page(client, config, start)
        if not page_rows:
            break
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
    return rows[:max_items]


def fetch_publication_detail(client: HttpClient, row: dict, config: dict) -> dict:
    detail_html = _get_page_html(client, row["detail_url"], config)
    field_pairs = re.findall(
        r'<div class="gsc_oci_field">(.*?)</div><div class="gsc_oci_value[^"]*">(.*?)</div>',
        detail_html,
        flags=re.S,
    )
    detail_fields = {_strip_tags(name).lower(): _strip_tags(value) for name, value in field_pairs}

    title_link_match = re.search(
        r'<div id="gsc_oci_title"><a class="gsc_oci_title_link" href="([^"]+)"',
        detail_html,
    )
    pdf_link_match = re.search(r"<span class='gsc_vcd_title_ggt'>\[PDF\]</span> from .*?</a>", detail_html)
    publisher_url = html.unescape(title_link_match.group(1)) if title_link_match else None

    return {
        **row,
        "detail_fields": detail_fields,
        "publisher_url": publisher_url,
        "has_pdf_link": bool(pdf_link_match),
    }
