from __future__ import annotations

import argparse
import getpass
import base64
import html
import json
import os
import socket
import subprocess
import sys
import shutil
from email.message import EmailMessage
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error

from .bibtex import (
    AGENT_PREFIX,
    build_updated_content,
    ensure_file_with_managed_block,
    extract_bib_entries,
    extract_managed_chunks,
    has_conflict_markers,
    remove_bib_entry,
    replace_managed_block,
    strip_managed_block,
    validate_rendered_chunks,
)
from .config import (
    active_bib_files,
    default_chrome_user_data_dir,
    detect_chrome_executable,
    load_config,
    resolve_path,
    resolve_routed_category,
    save_config,
)
from .http import HttpClient
from .metadata import bibtex_entry, enrich_record, extract_arxiv_id, extract_doi, make_bib_key, normalize_title
from .render import render_bibliography_pdf
from .scholar import fetch_profile_page, fetch_profile_rows, fetch_publication_detail, scholar_fetch_mode


def _load_state(path: Path) -> dict:
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    return {"bootstrap_completed_on": None, "post_cutoff_seen_ids": []}


def _save_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _existing_chunks_by_scholar_id(config: dict) -> dict[str, dict]:
    start_marker = config["managed_block"]["start_marker"]
    end_marker = config["managed_block"]["end_marker"]
    result = {}
    for _, bib_config in active_bib_files(config).items():
        path = resolve_path(config, bib_config["path"])
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        for chunk in extract_managed_chunks(content, start_marker, end_marker):
            scholar_id = chunk.metadata.get("scholar_id")
            if scholar_id:
                result[scholar_id] = {"metadata": chunk.metadata, "raw_entry": chunk.raw_entry}
    return result


def _assert_no_conflict_markers(config: dict) -> None:
    conflicted: list[str] = []
    for _, bib_config in active_bib_files(config).items():
        path = resolve_path(config, bib_config["path"])
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        if has_conflict_markers(content):
            conflicted.append(str(path))
    if conflicted:
        formatted = "\n".join(f"- {path}" for path in conflicted)
        raise RuntimeError(
            "Refusing to update because one or more bibliography files contain unresolved Git merge-conflict markers.\n"
            f"{formatted}\n"
            "Resolve the conflict markers first, then rerun the updater."
        )


def _existing_manual_entries(config: dict) -> list[dict]:
    start_marker = config["managed_block"]["start_marker"]
    end_marker = config["managed_block"]["end_marker"]
    results: list[dict] = []
    for category, bib_config in active_bib_files(config).items():
        path = resolve_path(config, bib_config["path"])
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8")
        manual_content = strip_managed_block(content, start_marker, end_marker)
        for entry in extract_bib_entries(manual_content):
            results.append(
                {
                    "category": category,
                    "path": str(path),
                    "key": entry.key,
                    "entry_type": entry.entry_type,
                    "raw_entry": entry.raw_entry,
                    "fields": entry.fields,
                }
            )
    return results


def _build_reconciliation_indexes(manual_entries: list[dict], agent_chunks: dict[str, dict]) -> dict:
    manual_by_doi: dict[str, list[dict]] = {}
    manual_by_arxiv: dict[str, list[dict]] = {}
    manual_by_title_year: dict[tuple[str, str], list[dict]] = {}
    manual_by_title: dict[str, list[dict]] = {}
    manual_surnames_by_title: dict[str, set[str]] = {}

    for entry in manual_entries:
        fields = entry["fields"]
        doi = extract_doi(fields.get("doi"), fields.get("url"))
        arxiv_id = extract_arxiv_id(fields.get("eprint"), fields.get("url"), fields.get("title"))
        title_norm = normalize_title(fields.get("title", ""))
        year = str(fields.get("year", "")).strip()
        if doi:
            manual_by_doi.setdefault(doi.lower(), []).append(entry)
        if arxiv_id:
            manual_by_arxiv.setdefault(arxiv_id.lower(), []).append(entry)
        if title_norm:
            manual_by_title.setdefault(title_norm, []).append(entry)
            if year:
                manual_by_title_year.setdefault((title_norm, year), []).append(entry)
            manual_surnames_by_title.setdefault(title_norm, set()).update(_author_surnames(fields.get("author", "")))

    return {
        "manual_by_doi": manual_by_doi,
        "manual_by_arxiv": manual_by_arxiv,
        "manual_by_title_year": manual_by_title_year,
        "manual_by_title": manual_by_title,
        "manual_surnames_by_title": manual_surnames_by_title,
        "agent_by_scholar_id": agent_chunks,
    }


def _collect_existing_keys(manual_entries: list[dict], agent_chunks: dict[str, dict]) -> set[str]:
    keys = {entry["key"] for entry in manual_entries if entry.get("key")}
    for chunk in agent_chunks.values():
        key = chunk.get("metadata", {}).get("key")
        if key:
            keys.add(key)
    return keys


def _author_surnames(author_field: str) -> set[str]:
    if not author_field:
        return set()
    parts = [part.strip() for part in author_field.split(" and ") if part.strip()]
    surnames = set()
    for part in parts:
        clean = part.replace("\\textbf{", "").replace("\\text{", "").replace("{", "").replace("}", "")
        tokens = [token for token in clean.replace(",", " ").split() if token]
        if tokens:
            surnames.add(tokens[-1].lower())
    return surnames


def _reconcile_record(scholar_id: str, enriched: dict, existing_chunks: dict[str, dict], indexes: dict) -> tuple[str, dict | None]:
    if scholar_id in existing_chunks:
        return "agent-existing", existing_chunks[scholar_id]

    doi = extract_doi(enriched.get("doi"), enriched.get("url"))
    arxiv_id = extract_arxiv_id(enriched.get("arxiv_id"), enriched.get("url"), enriched.get("title"))
    title_norm = normalize_title(enriched.get("title", ""))
    year = str(enriched.get("year") or "").strip()
    author_surnames = {surname.lower() for surname in _author_surnames(" and ".join(enriched.get("authors", [])))}

    if doi:
        matches = indexes["manual_by_doi"].get(doi.lower())
        if matches:
            return "manual-existing", matches[0]

    if arxiv_id:
        matches = indexes["manual_by_arxiv"].get(arxiv_id.lower())
        if matches:
            return "manual-existing", matches[0]

    if title_norm and year:
        matches = indexes["manual_by_title_year"].get((title_norm, year))
        if matches:
            return "manual-existing", matches[0]

    if title_norm:
        matches = indexes["manual_by_title"].get(title_norm)
        if matches:
            manual_surnames = indexes["manual_surnames_by_title"].get(title_norm, set())
            if author_surnames & manual_surnames:
                return "manual-existing", matches[0]
            return "possible-duplicate", matches[0]

    return "new", None


def _render_chunk(metadata: dict, entry_text: str) -> str:
    return f"{AGENT_PREFIX}{json.dumps(metadata, sort_keys=True)}\n{entry_text}"


def _stable_agent_metadata(metadata: dict) -> dict:
    stable = dict(metadata)
    stable.pop("status", None)
    return stable


def _is_manual_techreport_superseded(manual_entry: dict | None, publication_category: str) -> bool:
    if not manual_entry:
        return False
    if publication_category not in {"journal", "conference"}:
        return False
    manual_category = manual_entry.get("category")
    manual_type = (manual_entry.get("entry_type") or "").lower()
    return manual_category == "techreport" or manual_type in {"techreport", "misc", "unpublished", "report"}


def _write_report_files(config: dict, report: dict) -> None:
    reporting = config.get("reporting", {})
    json_path = resolve_path(config, reporting.get("json_report_file", "state/last_update_report.json"))
    text_path = resolve_path(config, reporting.get("text_report_file", "state/last_update_report.txt"))
    html_path = resolve_path(config, reporting.get("html_report_file", "state/last_update_report.html"))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")

    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(_format_text_report(report, int(reporting.get("max_listed_items", 20))), encoding="utf-8")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(_format_html_report(report, int(reporting.get("max_listed_items", 20))), encoding="utf-8")


def _notification_should_send(report: dict) -> bool:
    summary = report.get("summary", {})
    return bool(summary.get("new_entries", 0) or summary.get("updated_entries", 0))


def _default_from_address() -> str:
    return f"{getpass.getuser()}@{socket.getfqdn() or 'localhost'}"


def _notification_recipients(notifications: dict) -> list[str]:
    recipients = notifications.get("report_recipients")
    if recipients:
        return recipients
    email = notifications.get("email")
    return [email] if email else []


def _notification_from_address(notifications: dict) -> str:
    return notifications.get("report_from") or notifications.get("from_email") or _default_from_address()


def _build_notification_message(config: dict, report: dict) -> EmailMessage:
    notifications = config.get("notifications", {})
    reporting = config.get("reporting", {})
    text_path = resolve_path(config, reporting.get("text_report_file", "state/last_update_report.txt"))
    html_path = resolve_path(config, reporting.get("html_report_file", "state/last_update_report.html"))
    body = text_path.read_text(encoding="utf-8")
    html_body = html_path.read_text(encoding="utf-8") if html_path.exists() else None

    profile_id = config.get("scholar", {}).get("profile_id", "unknown-profile")
    subject_prefix = notifications.get("subject_prefix", "Bibliography Agent")
    subject = f"{subject_prefix}: changes detected for {profile_id} on {_today_iso()}"

    message = EmailMessage()
    message["To"] = ", ".join(_notification_recipients(notifications))
    message["From"] = _notification_from_address(notifications)
    message["Subject"] = subject
    message.set_content(body)
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return message


def _badge(label: str, value: str, tone: str) -> str:
    colors = {
        "blue": ("#e8f1ff", "#1f5fbf"),
        "green": ("#e9f8ef", "#16784a"),
        "amber": ("#fff4df", "#9a6200"),
        "gray": ("#eef1f4", "#5f6b76"),
        "red": ("#fdecec", "#b42318"),
    }
    bg, fg = colors.get(tone, colors["gray"])
    return (
        f"<span style=\"display:inline-block;padding:4px 8px;border-radius:999px;"
        f"font-size:12px;line-height:1;color:{fg};background:{bg};margin:0 8px 8px 0;\">"
        f"<strong>{html.escape(label)}:</strong> {html.escape(value)}</span>"
    )


def _shorten_path(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home) :]
    parts = path.split("/")
    if len(parts) <= 5:
        return path
    return "/".join(parts[:2] + ["..."] + parts[-2:])


def _section_card(title: str, body: str, accent: str = "#e7ebf0", background: str = "#ffffff") -> str:
    return (
        f"<div style=\"margin:0 0 16px 0;border:1px solid {accent};border-left:4px solid {accent};"
        f"background:{background};border-radius:12px;padding:14px 16px;\">"
        f"<div style=\"font-size:13px;font-weight:700;color:#344054;margin-bottom:8px;\">{html.escape(title)}</div>"
        f"{body}</div>"
    )


def _format_html_report(report: dict, max_listed_items: int) -> str:
    summary = report["summary"]
    fetch = report.get("fetch", {})
    files = report.get("files", [])
    changed_items = [item for item in report["items"] if item["status"] in {"new", "updated"}][:max_listed_items]
    duplicate_items = [item for item in report["items"] if item["status"] == "possible-duplicate"][:max_listed_items]
    changed_files = [item for item in files if item.get("changed")]

    def render_list(items: list[dict], fields: list[str], highlight: bool = False) -> str:
        if not items:
            return "<div style=\"color:#7a8591;font-size:12px;\">None</div>"
        rows = []
        for item in items:
            parts = [html.escape(str(item.get(field, ""))) for field in fields if item.get(field)]
            status = str(item.get("status", "")).upper()
            tone = "#e8f1ff" if item.get("status") == "new" else "#fff4df"
            left = f"border-left:3px solid {'#1f5fbf' if item.get('status') == 'new' else '#c77d00'};" if highlight else ""
            rows.append(
                f"<li style=\"margin:0 0 10px 0;list-style:none;padding:10px 12px;border-radius:10px;{left}"
                + (f"background:{tone};" if highlight else "background:#fafbfc;border:1px solid #edf0f3;")
                + "\">"
                + (f"<div style=\"font-size:11px;font-weight:700;color:#667085;margin-bottom:4px;\">{html.escape(status)}</div>" if status and highlight else "")
                + f"<span style=\"color:#111827;font-size:13px;\">{html.escape(item.get('title', 'Untitled'))}</span>"
                + (f"<div style=\"color:#667085;font-size:12px;margin-top:2px;\">{list_separator.join(parts)}</div>" if parts else "")
                + "</li>"
            )
        return f"<ul style=\"margin:8px 0 0 0;padding:0;\">{''.join(rows)}</ul>"

    file_rows = "".join(
        (
            f"<tr style=\"background:{'#eef6ff' if item['changed'] else '#ffffff'};\">"
            f"<td style=\"padding:8px 10px;border-top:1px solid #edf0f3;color:#111827;font-weight:{'600' if item['changed'] else '400'};\">{html.escape(item['category'])}</td>"
            f"<td style=\"padding:8px 10px;border-top:1px solid #edf0f3;color:#667085;font-size:12px;\">{html.escape(_shorten_path(item['path']))}</td>"
            f"<td style=\"padding:8px 10px;border-top:1px solid #edf0f3;color:{'#1f5fbf' if item['changed'] else '#111827'};text-align:center;font-weight:{'600' if item['changed'] else '400'};\">{'yes' if item['changed'] else 'no'}</td>"
            f"<td style=\"padding:8px 10px;border-top:1px solid #edf0f3;color:#111827;text-align:center;\">{item['managed_entry_count']}</td>"
            "</tr>"
        )
        for item in files
    )

    separator = "&middot;"
    list_separator = " &middot; "
    header_badges = (
        _badge("Changed", "YES" if report["changed"] else "NO", "green" if report["changed"] else "gray")
        + _badge("New", str(summary["new_entries"]), "blue")
        + _badge("Updated", str(summary["updated_entries"]), "amber")
        + _badge("Mode", fetch.get("mode", "unknown"), "gray")
    )
    summary_body = f"""
      <div style="font-size:12px;line-height:1.6;color:#475467;">
        Rows fetched: <strong>{fetch.get('row_count', 0)}</strong> &nbsp;{separator}&nbsp;
        Selected: <strong>{fetch.get('selected_count', 0)}</strong> &nbsp;{separator}&nbsp;
        Files changed: <strong>{summary['changed_file_count']}</strong> &nbsp;{separator}&nbsp;
        Possible duplicates: <strong>{summary['possible_duplicates']}</strong>
      </div>
      """
    summary_card = _section_card("Summary", summary_body, accent="#e7ebf0", background="#fafbfc")
    added_body = f"<div style=\"font-size:12px;color:#475467;\">{render_list(changed_items, ['category', 'key'], highlight=True)}</div>"
    added_card = _section_card("Added Or Updated", added_body, accent="#d7e7ff", background="#fcfdff")
    if changed_files:
        changed_files_body = "".join(
            (
                f"<div style=\"padding:10px 12px;margin:0 0 8px 0;border-radius:10px;background:#eef6ff;border-left:3px solid #1f5fbf;\">"
                f"<div style=\"font-size:13px;font-weight:600;color:#111827;\">{html.escape(item['category'])}</div>"
                f"<div style=\"font-size:12px;color:#667085;margin-top:2px;\">{html.escape(_shorten_path(item['path']))}</div>"
                f"<div style=\"font-size:12px;color:#344054;margin-top:4px;\">managed entries: <strong>{item['managed_entry_count']}</strong>"
                f" &nbsp;{separator}&nbsp; manual removals: <strong>{item.get('removed_manual_entry_count', 0)}</strong></div>"
                "</div>"
            )
            for item in changed_files
        )
    else:
        changed_files_body = "<div style=\"color:#7a8591;font-size:12px;\">None</div>"
    changed_files_card = _section_card("Changed Files", changed_files_body, accent="#d7e7ff", background="#fcfdff")
    all_files_body = f"""
      <table style="width:100%;border-collapse:collapse;font-size:12px;">
        <thead>
          <tr style="background:#f8fafc;color:#667085;text-align:left;">
            <th style="padding:8px 10px;">Category</th>
            <th style="padding:8px 10px;">Path</th>
            <th style="padding:8px 10px;text-align:center;">Changed</th>
            <th style="padding:8px 10px;text-align:center;">Managed</th>
          </tr>
        </thead>
        <tbody>{file_rows}</tbody>
      </table>
      """
    all_files_card = _section_card("All Files", all_files_body, accent="#e7ebf0", background="#ffffff")
    duplicate_body = f"<div style=\"font-size:12px;color:#475467;\">{render_list(duplicate_items, ['category', 'matched_key'])}</div>"
    duplicate_card = _section_card("Possible Duplicates", duplicate_body, accent="#f1e6c8", background="#fffcf6")

    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:24px;background:#f6f8fb;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:820px;margin:0 auto;background:#ffffff;border:1px solid #e7ebf0;border-radius:16px;overflow:hidden;">
    <div style="padding:22px 24px 16px;background:linear-gradient(180deg,#fbfcfe 0%,#f4f7fb 100%);border-bottom:1px solid #e7ebf0;">
      <div style="font-size:22px;font-weight:700;color:#101828;">Bibliography Agent Report</div>
      <div style="font-size:12px;color:#667085;margin-top:4px;">{html.escape(report['date'])}</div>
      <div style="margin-top:14px;">
        {header_badges}
      </div>
    </div>

    <div style="padding:18px 24px 8px;">
      {summary_card}
      {added_card}
      {changed_files_card}
      {all_files_card}
      {duplicate_card}
    </div>
  </div>
</body>
</html>"""


def _gmail_api_send(config: dict, message: EmailMessage) -> None:
    notifications = config.get("notifications", {})
    token_path = resolve_path(config, notifications.get("gmail_token_file", "config/gmail_token.json"))
    creds_path = resolve_path(config, notifications.get("gmail_creds_file", "config/gmail_credentials.json"))
    if not token_path.exists():
        raise RuntimeError(f"Gmail token file not found: {token_path}")
    token_data = json.loads(token_path.read_text(encoding="utf-8"))
    if creds_path.exists():
        creds_data = json.loads(creds_path.read_text(encoding="utf-8"))
        installed = creds_data.get("installed", {})
        token_data.setdefault("client_id", installed.get("client_id"))
        token_data.setdefault("client_secret", installed.get("client_secret"))
        token_data.setdefault("token_uri", installed.get("token_uri"))

    token_uri = token_data.get("token_uri", "https://oauth2.googleapis.com/token")
    refresh_payload = urllib.parse.urlencode(
        {
            "client_id": token_data["client_id"],
            "client_secret": token_data["client_secret"],
            "refresh_token": token_data["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    refresh_request = urllib.request.Request(
        token_uri,
        data=refresh_payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(refresh_request, timeout=20) as response:
            refresh_result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gmail API token refresh failed: HTTP {exc.code} {exc.reason}\n{body}") from exc

    token_data["token"] = refresh_result["access_token"]
    if "expires_in" in refresh_result:
        from datetime import datetime, timedelta, timezone

        expiry = datetime.now(timezone.utc) + timedelta(seconds=int(refresh_result["expires_in"]))
        token_data["expiry"] = expiry.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    token_path.write_text(json.dumps(token_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    sender = notifications.get("gmail_sender") or "me"
    send_request = urllib.request.Request(
        f"https://gmail.googleapis.com/gmail/v1/users/{urllib.parse.quote(sender, safe='')}/messages/send",
        data=json.dumps({"raw": raw}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token_data['token']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(send_request, timeout=20) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gmail API send failed: HTTP {exc.code} {exc.reason}\n{body}") from exc


def _send_report_email(config: dict, report: dict) -> bool:
    notifications = config.get("notifications", {})
    if not notifications.get("enabled", False):
        return False
    if not _notification_should_send(report):
        return False
    if not _notification_recipients(notifications):
        return False

    message = _build_notification_message(config, report)
    transport = notifications.get("transport", "sendmail")

    if transport == "gmail_api":
        _gmail_api_send(config, message)
        return True

    sendmail_path = notifications.get("sendmail_path", "/usr/sbin/sendmail")
    completed = subprocess.run(
        [sendmail_path, "-t", "-oi"],
        input=message.as_string(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Failed to send update email.\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return True


def _git_repo_root_for_path(path: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(Path(path).parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _auto_commit_changed_bibs(config: dict, file_reports: list[dict]) -> list[str]:
    git_config = config.get("git", {})
    if not git_config.get("auto_commit_changed_bibs", False):
        return []

    changed_paths = [item["path"] for item in file_reports if item.get("changed")]
    if not changed_paths:
        return []

    repo_to_paths: dict[str, list[str]] = {}
    for path in changed_paths:
        repo_root = _git_repo_root_for_path(path)
        if repo_root:
            repo_to_paths.setdefault(repo_root, []).append(path)

    commits: list[str] = []
    for repo_root, paths in repo_to_paths.items():
        rel_paths = [str(Path(path).resolve().relative_to(Path(repo_root).resolve())) for path in paths]
        message = git_config.get(
            "auto_commit_message",
            f"Bib Agent update {_today_iso()}",
        )
        stage_completed = subprocess.run(
            ["git", "-C", repo_root, "add", "--", *rel_paths],
            capture_output=True,
            text=True,
            check=False,
        )
        if stage_completed.returncode != 0:
            raise RuntimeError(
                "Automatic bib staging failed.\n"
                f"repo: {repo_root}\n"
                f"stdout:\n{stage_completed.stdout}\n"
                f"stderr:\n{stage_completed.stderr}"
            )

        diff_completed = subprocess.run(
            ["git", "-C", repo_root, "diff", "--cached", "--quiet", "--", *rel_paths],
            capture_output=True,
            text=True,
            check=False,
        )
        if diff_completed.returncode == 0:
            continue
        if diff_completed.returncode not in {0, 1}:
            raise RuntimeError(
                "Automatic bib pre-commit check failed.\n"
                f"repo: {repo_root}\n"
                f"stdout:\n{diff_completed.stdout}\n"
                f"stderr:\n{diff_completed.stderr}"
            )

        commit_completed = subprocess.run(
            ["git", "-C", repo_root, "commit", "-m", message, "--", *rel_paths],
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_completed.returncode == 0:
            commits.append(f"{repo_root}: {message}")
            continue
        if (
            "no changes added to commit" in commit_completed.stderr.lower()
            or "nothing to commit" in commit_completed.stdout.lower()
        ):
            continue
        raise RuntimeError(
            "Automatic bib commit failed.\n"
            f"repo: {repo_root}\n"
            f"stdout:\n{commit_completed.stdout}\n"
            f"stderr:\n{commit_completed.stderr}"
        )
    return commits


def _format_text_report(report: dict, max_listed_items: int) -> str:
    summary = report["summary"]
    fetch = report.get("fetch", {})
    baseline = report.get("baseline", {})
    lines = [
        "Bibliography Agent Report",
        f"Date: {report['date']}",
        f"Changed: {'YES' if report['changed'] else 'NO'}",
        "",
        "Fetch",
        f"- Mode: {fetch.get('mode', 'unknown')}",
        f"- Rows fetched from Scholar: {fetch.get('row_count', 0)}",
        f"- Rows selected for deeper reconciliation: {fetch.get('selected_count', 0)}",
        f"- Pages fetched: {fetch.get('page_count', 0)}",
        f"- Seen post-cutoff ids in state: {fetch.get('known_post_cutoff_count', 0)}",
        f"- Baseline source: {baseline.get('source', 'unknown')}",
        f"- Candidate rule: year > {baseline.get('cutoff_year', 'unknown')} and not already in manual bibs",
        "",
        "Summary",
        f"- Managed entries written: {summary['managed_entries_written']}",
        f"- Bib files changed: {summary['changed_file_count']}",
        f"- New entries: {summary['new_entries']}",
        f"- Updated agent entries: {summary['updated_entries']}",
        f"- Unchanged agent entries: {summary['unchanged_agent_entries']}",
        f"- Manual-existing matches skipped: {summary['manual_existing']}",
        f"- Possible duplicates skipped: {summary['possible_duplicates']}",
        f"- Superseded manual tech reports promoted: {summary.get('superseded_manual_techreports', 0)}",
        f"- Pre-cutoff entries ignored: {summary['old_entries']}",
        "",
        "Files",
    ]

    for item in report["files"]:
        lines.append(
            f"- {item['category']}: {'changed' if item['changed'] else 'no change'} "
            f"({item['path']}, managed entries={item['managed_entry_count']}, "
            f"manual removals={item.get('removed_manual_entry_count', 0)})"
        )

    sampled_rows = fetch.get("sampled_rows", [])[:max_listed_items]
    if sampled_rows:
        lines.extend(["", "Fetched Rows"])
        for row in sampled_rows:
            lines.append(
                f"- [{row['status']}] {row['title']} "
                f"(year={row.get('year')}, scholar_id={row['scholar_id']})"
            )

    listed_changes = [item for item in report["items"] if item["status"] in {"new", "updated"}][:max_listed_items]
    if listed_changes:
        lines.extend(["", "Added Or Updated"])
        for item in listed_changes:
            lines.append(f"- [{item['status']}] {item['title']} ({item['category']}, key={item['key']})")

    manual_existing = [item for item in report["items"] if item["status"] == "manual-existing"][:max_listed_items]
    if manual_existing:
        lines.extend(["", "Skipped As Manual Existing"])
        for item in manual_existing:
            matched = item.get("matched_key")
            suffix = f", matched={matched}" if matched else ""
            lines.append(f"- {item['title']} ({item['category']}{suffix})")

    if report["summary"]["possible_duplicates"] > 0:
        possible_duplicates = [item for item in report["items"] if item["status"] == "possible-duplicate"][:max_listed_items]
        lines.extend(["", "Possible Duplicates"])
        for item in possible_duplicates:
            matched = item.get("matched_key")
            suffix = f", matched={matched}" if matched else ""
            lines.append(f"- {item['title']} ({item['category']}{suffix})")

    old_items = [item for item in report["items"] if item["status"] == "old"][:max_listed_items]
    if old_items:
        lines.extend(["", "Skipped As Pre-Cutoff"])
        for item in old_items:
            lines.append(f"- {item['title']} (year={item.get('year')}, scholar_id={item['scholar_id']})")

    return "\n".join(lines) + "\n"


def bootstrap(config_path: str) -> None:
    config = load_config(config_path)
    client = HttpClient(
        min_interval_seconds=float(config["scholar"].get("min_request_interval_seconds", 1.0)),
        timeout_seconds=int(config["scholar"].get("request_timeout_seconds", 20)),
    )
    rows = fetch_profile_rows(client, config)
    state_path = resolve_path(config, config["state_file"])
    state = _load_state(state_path)
    state["bootstrap_completed_on"] = _today_iso()
    state["baseline"] = {
        "source": config.get("baseline", {}).get("source", "manual_bibs"),
        "cutoff_year": config.get("baseline", {}).get("cutoff_year", 2025),
        "manual_entry_count": len(_existing_manual_entries(config)),
        "scholar_row_count": len(rows),
    }
    _save_state(state_path, state)

    for _, bib_config in active_bib_files(config).items():
        ensure_file_with_managed_block(
            resolve_path(config, bib_config["path"]),
            config["managed_block"]["start_marker"],
            config["managed_block"]["end_marker"],
        )

    print(f"Bootstrapped manual-bib baseline with {len(rows)} Scholar rows observed into {state_path}")


def auth_bootstrap(config_path: str) -> None:
    config = load_config(config_path)
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
    user_data_dir_value = auth_config.get("chrome_user_data_dir")
    if user_data_dir_value:
        user_data_dir = resolve_path(config, user_data_dir_value)
    else:
        user_data_dir = default_chrome_user_data_dir()
    command = [
        "node",
        str(browser_script),
        "bootstrap-from-profile",
        "--url",
        "https://scholar.google.com/citations?hl=en&user="
        + config["scholar"]["profile_id"]
        + "&view_op=list_works&sortby=pubdate",
        "--storage-state",
        str(storage_state),
        "--chrome-executable",
        str(chrome_path),
        "--chrome-user-data-dir",
        str(user_data_dir),
        "--chrome-profile-directory",
        auth_config.get("chrome_profile_directory", "Default"),
        "--headless",
        "true" if auth_config.get("headless", True) else "false",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            "Scholar auth bootstrap failed. "
            "If Chrome is running, close it first and rerun.\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    print(completed.stdout.strip() or f"Saved Scholar session to {storage_state}")


def update(config_path: str) -> None:
    config = load_config(config_path)
    active_targets = active_bib_files(config)
    _assert_no_conflict_markers(config)
    client = HttpClient(
        min_interval_seconds=float(config["scholar"].get("min_request_interval_seconds", 1.0)),
        timeout_seconds=int(config["scholar"].get("request_timeout_seconds", 20)),
    )
    state_path = resolve_path(config, config["state_file"])
    state = _load_state(state_path)
    start_marker = config["managed_block"]["start_marker"]
    end_marker = config["managed_block"]["end_marker"]
    existing_chunks = _existing_chunks_by_scholar_id(config)
    manual_entries = _existing_manual_entries(config)
    indexes = _build_reconciliation_indexes(manual_entries, existing_chunks)
    existing_keys = _collect_existing_keys(manual_entries, existing_chunks)
    existing_agent_ids = set(existing_chunks)
    cutoff_year = int(config.get("baseline", {}).get("cutoff_year", 2025))
    stop_after_known_pages = int(config.get("baseline", {}).get("stop_after_known_pages", 1))
    known_post_cutoff_ids = set(state.get("post_cutoff_seen_ids", [])) | existing_agent_ids

    for _, bib_config in active_targets.items():
        ensure_file_with_managed_block(resolve_path(config, bib_config["path"]), start_marker, end_marker)

    rows = []
    page_count = 0
    consecutive_known_pages = 0
    page_size = int(config["scholar"].get("page_size", 100))
    max_items = int(config["scholar"].get("max_items", 300))
    for start in range(0, max_items, page_size):
        page_rows = fetch_profile_page(client, config, start)
        if not page_rows:
            break
        page_count += 1
        rows.extend(page_rows)
        post_cutoff_page_rows = [row for row in page_rows if (row.get("year") or 0) > cutoff_year]
        if not post_cutoff_page_rows:
            break
        if all(row["scholar_id"] in known_post_cutoff_ids for row in post_cutoff_page_rows):
            consecutive_known_pages += 1
        else:
            consecutive_known_pages = 0
        if consecutive_known_pages >= stop_after_known_pages:
            break
        if len(page_rows) < page_size:
            break
    live_rows = {row["scholar_id"]: row for row in rows}
    selected_ids = []
    reconciliation_summary = {
        "old": 0,
        "new": 0,
        "manual-existing": 0,
        "agent-existing": 0,
        "updated": 0,
        "unchanged-agent": 0,
        "possible-duplicate": 0,
        "superseded-manual-techreport": 0,
    }
    report_items: list[dict] = []
    for row in rows:
        scholar_id = row["scholar_id"]
        row_year = row.get("year")
        if scholar_id in existing_agent_ids:
            selected_ids.append(scholar_id)
        else:
            if row_year is not None and row_year > cutoff_year:
                selected_ids.append(scholar_id)
            else:
                reconciliation_summary["old"] += 1
                report_items.append(
                    {
                        "scholar_id": scholar_id,
                        "title": row.get("title"),
                        "category": None,
                        "status": "old",
                        "year": row_year,
                        "reason": f"year <= cutoff_year ({cutoff_year})",
                    }
                )

    rendered_by_category: dict[str, list[str]] = {name: [] for name in active_targets}
    manual_removals_by_path: dict[str, list[dict]] = {}
    touched_ids: set[str] = set()
    for scholar_id in selected_ids:
        detail = fetch_publication_detail(client, live_rows[scholar_id], config)
        enriched = enrich_record(client, detail, config["publisher_metadata"].get("max_search_results", 5))
        publication_category = enriched["category"]
        routed_category = resolve_routed_category(config, publication_category)
        enriched["category"] = routed_category
        status, matched_entry = _reconcile_record(scholar_id, enriched, existing_chunks, indexes)
        superseded_manual_techreport = False
        if status in {"manual-existing", "possible-duplicate"} and _is_manual_techreport_superseded(
            matched_entry, publication_category
        ):
            superseded_manual_techreport = True
            reconciliation_summary["superseded-manual-techreport"] += 1
            status = "new"
        reconciliation_summary[status] = reconciliation_summary.get(status, 0) + 1
        item_report = {
            "scholar_id": scholar_id,
            "title": enriched.get("title"),
            "category": routed_category,
            "status": status,
            "year": enriched.get("year"),
        }
        if matched_entry:
            item_report["matched_key"] = matched_entry.get("key")
            item_report["matched_path"] = matched_entry.get("path")
        if superseded_manual_techreport and matched_entry:
            manual_removals_by_path.setdefault(matched_entry["path"], []).append(matched_entry)
            item_report["superseded_key"] = matched_entry.get("key")
            item_report["superseded_path"] = matched_entry.get("path")
        if status in {"manual-existing", "possible-duplicate"}:
            if status == "manual-existing":
                item_report["reason"] = "matched an existing manual bib entry"
            else:
                item_report["reason"] = "weak title-only duplicate match against a manual bib entry"
            report_items.append(item_report)
            continue
        existing_key = existing_chunks.get(scholar_id, {}).get("metadata", {}).get("key")
        key_year = live_rows[scholar_id].get("year") or enriched.get("year")
        key = make_bib_key(
            key_year,
            existing_keys,
            config.get("key_generation", {}),
            existing_key,
        )
        entry_text, _ = bibtex_entry(enriched, key, config["author_emphasis"])
        metadata = {
            "category": enriched["category"],
            "key": key,
            "scholar_id": scholar_id,
            "source": "doi" if enriched.get("doi") else ("arxiv" if enriched.get("arxiv_id") else "scholar"),
            "doi": enriched.get("doi"),
            "arxiv_id": enriched.get("arxiv_id"),
            "title_fingerprint": normalize_title(enriched.get("title", "")),
        }
        if status == "new":
            item_report["status"] = "new"
            if superseded_manual_techreport and matched_entry:
                item_report["reason"] = (
                    "manual tech-report entry was superseded by a published journal/conference version; "
                    "old manual entry removed"
                )
            else:
                item_report["reason"] = (
                    f"year > cutoff_year ({cutoff_year}) and not matched to any existing manual bib entry"
                )
        elif status == "agent-existing":
            item_report["reason"] = "exact scholar_id match to an existing agent-managed entry"
            existing_raw = existing_chunks[scholar_id]["raw_entry"].strip()
            if existing_raw != entry_text.strip():
                reconciliation_summary["updated"] += 1
                item_report["status"] = "updated"
            else:
                reconciliation_summary["unchanged-agent"] += 1
                item_report["status"] = "unchanged-agent"
        item_report["key"] = key
        rendered_by_category[routed_category].append(_render_chunk(_stable_agent_metadata(metadata), entry_text))
        report_items.append(item_report)
        touched_ids.add(scholar_id)

    state["post_cutoff_seen_ids"] = sorted(
        known_post_cutoff_ids
        | {
            row["scholar_id"]
            for row in rows
            if (row.get("year") or 0) > cutoff_year
        }
    )

    if config["update_policy"].get("keep_agent_entries_missing_from_profile", True):
        for scholar_id, existing in existing_chunks.items():
            if scholar_id in touched_ids or scholar_id in live_rows:
                continue
            category = existing["metadata"].get("category", resolve_routed_category(config, "techreport"))
            rendered_by_category.setdefault(category, []).append(
                _render_chunk(_stable_agent_metadata(existing["metadata"]), existing["raw_entry"])
            )

    file_reports = []
    for category, bib_config in active_targets.items():
        rendered = rendered_by_category.get(category, [])
        validate_rendered_chunks(rendered)
        path = resolve_path(config, bib_config["path"])
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        working = before
        removed_manual_entries = 0
        for manual_entry in manual_removals_by_path.get(str(path), []):
            updated = remove_bib_entry(working, manual_entry["raw_entry"])
            if updated != working:
                removed_manual_entries += 1
                working = updated
        after = build_updated_content(working, start_marker, end_marker, rendered)
        changed = before != after
        path.write_text(after, encoding="utf-8")
        file_reports.append(
            {
                "category": category,
                "path": str(path),
                "changed": changed,
                "managed_entry_count": len(rendered),
                "removed_manual_entry_count": removed_manual_entries,
            }
        )

    state["last_reconciliation"] = {
        "date": _today_iso(),
        "summary": reconciliation_summary,
    }
    _save_state(state_path, state)

    report = {
        "date": _today_iso(),
        "changed": any(item["changed"] for item in file_reports),
        "baseline": {
            "source": config.get("baseline", {}).get("source", "manual_bibs"),
            "cutoff_year": cutoff_year,
            "manual_entry_count": len(manual_entries),
        },
        "fetch": {
            "mode": scholar_fetch_mode(config),
            "row_count": len(rows),
            "selected_count": len(selected_ids),
            "page_count": page_count,
            "known_post_cutoff_count": len(state.get("post_cutoff_seen_ids", [])),
            "sampled_rows": [
                {
                    "scholar_id": item["scholar_id"],
                    "title": item["title"],
                    "status": item["status"],
                    "year": item.get("year"),
                }
                for item in report_items[:50]
            ],
        },
        "summary": {
            "managed_entries_written": sum(len(entries) for entries in rendered_by_category.values()),
            "changed_file_count": sum(1 for item in file_reports if item["changed"]),
            "new_entries": sum(1 for item in report_items if item["status"] == "new"),
            "updated_entries": sum(1 for item in report_items if item["status"] == "updated"),
            "unchanged_agent_entries": sum(1 for item in report_items if item["status"] == "unchanged-agent"),
            "manual_existing": reconciliation_summary["manual-existing"],
            "possible_duplicates": reconciliation_summary["possible-duplicate"],
            "superseded_manual_techreports": reconciliation_summary["superseded-manual-techreport"],
            "old_entries": reconciliation_summary["old"],
        },
        "files": file_reports,
        "items": report_items,
        "reconciliation": reconciliation_summary,
    }
    _write_report_files(config, report)
    commit_notes = _auto_commit_changed_bibs(config, file_reports)
    email_sent = _send_report_email(config, report)

    print(
        (
            _format_text_report(report, 5).strip()
            + (("\n\nGit auto-commit:\n- " + "\n- ".join(commit_notes)) if commit_notes else "")
            + ("\n\nEmail notification: sent" if email_sent else "")
        )
    )


def _today_iso() -> str:
    from datetime import date

    return date.today().isoformat()


def _platform_defaults() -> dict:
    if sys.platform == "darwin":
        return {
            "chrome_executable_candidates": [str(detect_chrome_executable() or "")],
            "chrome_user_data_dir": str(default_chrome_user_data_dir()),
            "sendmail_candidates": ["/usr/sbin/sendmail", "/usr/lib/sendmail"],
        }
    if sys.platform.startswith("linux"):
        return {
            "chrome_executable_candidates": [str(detect_chrome_executable() or "")],
            "chrome_user_data_dir": str(default_chrome_user_data_dir()),
            "sendmail_candidates": ["/usr/sbin/sendmail", "/usr/lib/sendmail", shutil.which("sendmail") or ""],
        }
    return {
        "chrome_executable_candidates": [str(detect_chrome_executable() or "")],
        "chrome_user_data_dir": str(default_chrome_user_data_dir()),
        "sendmail_candidates": [shutil.which("sendmail") or ""],
    }


def _first_existing_path(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate and Path(candidate).expanduser().exists():
            return str(Path(candidate).expanduser())
    return None


def precheck(config_path: str, write: bool = False) -> None:
    config = load_config(config_path)
    defaults = _platform_defaults()
    findings: list[str] = []
    fixes: list[str] = []

    auth = config.setdefault("auth", {})
    notifications = config.setdefault("notifications", {})

    chrome_executable = auth.get("chrome_executable")
    chrome_user_data_dir = auth.get("chrome_user_data_dir")
    sendmail_path = notifications.get("sendmail_path")

    detected_chrome = _first_existing_path(defaults["chrome_executable_candidates"])
    if not chrome_executable:
        if detected_chrome:
            findings.append(f"Chrome executable auto-detect available: {detected_chrome}")
        else:
            findings.append("Chrome executable not configured and auto-detect failed")
    elif not resolve_path(config, chrome_executable).exists():
        findings.append(f"Chrome executable missing or invalid: {chrome_executable}")
        if detected_chrome:
            fixes.append(f"Set auth.chrome_executable -> {detected_chrome}")
            if write:
                auth["chrome_executable"] = detected_chrome
    else:
        findings.append(f"Chrome executable OK: {resolve_path(config, chrome_executable)}")

    if not chrome_user_data_dir:
        findings.append(f"Chrome user data dir auto-default: {defaults['chrome_user_data_dir']}")
    elif not Path(chrome_user_data_dir).expanduser().exists():
        findings.append(f"Chrome user data dir missing or invalid: {chrome_user_data_dir}")
        default_user_data = defaults["chrome_user_data_dir"]
        fixes.append(f"Set auth.chrome_user_data_dir -> {default_user_data}")
        if write:
            auth["chrome_user_data_dir"] = default_user_data
    else:
        findings.append(f"Chrome user data dir OK: {Path(chrome_user_data_dir).expanduser()}")

    detected_sendmail = _first_existing_path(defaults["sendmail_candidates"])
    if notifications.get("transport", "sendmail") == "sendmail":
        if not sendmail_path or not Path(sendmail_path).expanduser().exists():
            findings.append(f"sendmail path missing or invalid: {sendmail_path}")
            if detected_sendmail:
                fixes.append(f"Set notifications.sendmail_path -> {detected_sendmail}")
                if write:
                    notifications["sendmail_path"] = detected_sendmail
        else:
            findings.append(f"sendmail path OK: {Path(sendmail_path).expanduser()}")

    for label in ["gmail_token_file", "gmail_creds_file"]:
        if label in notifications:
            resolved = resolve_path(config, notifications[label])
            if resolved.exists():
                findings.append(f"{label} OK: {resolved}")
            else:
                findings.append(f"{label} missing: {resolved}")

    for name, bib_config in active_bib_files(config).items():
        bib_path = resolve_path(config, bib_config["path"])
        if bib_path.exists():
            findings.append(f"bib_files.{name} OK: {bib_path}")
        else:
            findings.append(f"bib_files.{name} missing: {bib_path}")

    for binary in ["python3", "node", "npm"]:
        findings.append(f"{binary}: {shutil.which(binary) or 'not found'}")

    if write and fixes:
        save_config(config, config_path)

    print("Bibliography Agent Precheck")
    print(f"Platform: {sys.platform}")
    print("")
    print("Checks")
    for finding in findings:
        print(f"- {finding}")
    if fixes:
        print("")
        print("Suggested Fixes")
        for fix in fixes:
            print(f"- {fix}")
    if write:
        print("")
        print("Config write: completed" if fixes else "Config write: no changes needed")
    else:
        print("")
        print("Run `python3 update_bibs.py precheck --write` to apply detected safe defaults.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Google Scholar driven BibTeX updater.")
    parser.add_argument("command", choices=["auth-bootstrap", "bootstrap", "update", "render-pdf", "precheck"])
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    if args.command == "auth-bootstrap":
        auth_bootstrap(args.config)
    elif args.command == "bootstrap":
        bootstrap(args.config)
    elif args.command == "precheck":
        precheck(args.config, write=args.write)
    elif args.command == "render-pdf":
        config = load_config(args.config)
        pdf_path = render_bibliography_pdf(config, args.output_dir)
        print(f"Rendered bibliography PDF: {pdf_path}")
    else:
        update(args.config)


if __name__ == "__main__":
    main()
