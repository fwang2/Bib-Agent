import unittest
from pathlib import Path

from bib_agent.config import active_bib_files, resolve_routed_category
from bib_agent.bibtex import extract_bib_entries, extract_managed_chunks, inject_managed_block_if_missing, remove_bib_entry, strip_managed_block, validate_rendered_chunks
from bib_agent.cli import _build_notification_message, _format_html_report, _format_text_report, _is_manual_techreport_superseded, _notification_should_send
from bib_agent.metadata import _crossref_search, emphasize_authors, enrich_record, make_bib_key
from bib_agent.render import _render_tex


class BibtexTests(unittest.TestCase):
    def test_managed_block_injection(self):
        content = "@article{manual,\n  title = {Manual}\n}\n"
        updated = inject_managed_block_if_missing(
            content,
            "% >>> BIB_AGENT_MANAGED_START >>>",
            "% <<< BIB_AGENT_MANAGED_END <<<",
        )
        self.assertIn("@article{manual", updated)
        self.assertIn("% >>> BIB_AGENT_MANAGED_START >>>", updated)
        self.assertIn("% <<< BIB_AGENT_MANAGED_END <<<", updated)

    def test_extract_managed_chunks(self):
        content = """% >>> BIB_AGENT_MANAGED_START >>>
% Agent-managed entries live only inside this block.
% BIB_AGENT {"category": "conference", "key": "yin2025ringx", "scholar_id": "abc"}
@inproceedings{yin2025ringx,
  title = {RingX}
}
% <<< BIB_AGENT_MANAGED_END <<<
"""
        chunks = extract_managed_chunks(
            content,
            "% >>> BIB_AGENT_MANAGED_START >>>",
            "% <<< BIB_AGENT_MANAGED_END <<<",
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].metadata["scholar_id"], "abc")
        self.assertIn("@inproceedings{yin2025ringx,", chunks[0].raw_entry)

    def test_validate_rendered_chunks_rejects_duplicate_keys(self):
        duplicate = [
            '% BIB_AGENT {"key":"same"}\n@article{same,\n  title = {A}\n}',
            '% BIB_AGENT {"key":"same"}\n@article{same,\n  title = {B}\n}',
        ]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_rendered_chunks(duplicate)

    def test_strip_managed_block_and_parse_entries(self):
        content = """% >>> BIB_AGENT_MANAGED_START >>>
% Agent-managed entries live only inside this block.
% BIB_AGENT {"category": "conference", "key": "managed", "scholar_id": "abc"}
@inproceedings{managed,
  title = {Managed}
}
% <<< BIB_AGENT_MANAGED_END <<<

@techreport{manual,
  title = {A Report on Simulation-Driven Reliability and Failure
Analysis of Large-Scale Storage Systems},
  doi = {10.1234/example},
  year = {2014}
}
"""
        manual = strip_managed_block(
            content,
            "% >>> BIB_AGENT_MANAGED_START >>>",
            "% <<< BIB_AGENT_MANAGED_END <<<",
        )
        entries = extract_bib_entries(manual)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].key, "manual")
        self.assertEqual(entries[0].fields["year"], "2014")
        self.assertIn("Simulation-Driven", entries[0].fields["title"])

    def test_remove_bib_entry_removes_only_target_entry(self):
        content = (
            "@misc{old,\n  title = {Old}\n}\n\n"
            "@article{keep,\n  title = {Keep}\n}\n"
        )
        updated = remove_bib_entry(content, "@misc{old,\n  title = {Old}\n}")
        self.assertNotIn("@misc{old", updated)
        self.assertIn("@article{keep", updated)

    def test_make_bib_key_uses_incremental_year_suffix(self):
        existing = {"f7b-2026a", "f7b-2026b", "fwang2:2025a"}
        key = make_bib_key(2026, existing, {"prefix": "f7b", "separator": "-"})
        self.assertEqual(key, "f7b-2026c")
        second = make_bib_key(2026, existing, {"prefix": "f7b", "separator": "-"})
        self.assertEqual(second, "f7b-2026d")

    def test_emphasize_authors_preserves_original_name_order(self):
        rendered = emphasize_authors(
            ["Wang, Feiyi", "Lu, Hao"],
            {
                "preserve_original_format": True,
                "target_names": ["Feiyi Wang", "F Wang", "F. Wang"],
                "render_as": r"\textbf{Feiyi Wang}",
            },
        )
        self.assertIn(r"\textbf{Wang, Feiyi}", rendered)
        self.assertNotIn(r"{\textbf{Feiyi Wang}}", rendered)

    def test_emphasize_authors_matches_initial_variants_without_extra_braces(self):
        rendered = emphasize_authors(
            ["F. Wang", "Alice Smith"],
            {
                "preserve_original_format": True,
                "target_names": ["Feiyi Wang", "F Wang", "F. Wang"],
                "render_as": r"\textbf{Feiyi Wang}",
            },
        )
        self.assertIn(r"\textbf{F. Wang}", rendered)
        self.assertNotIn(r"{\textbf", rendered)

    def test_enrich_record_prefers_scholar_year_over_crossref_year(self):
        class StubClient:
            def get_json(self, url):
                return {}
            def get_text(self, url, headers=None):
                return ""

        record = {
            "title": "Accelerating dataset distillation via model augmentation",
            "year": 2026,
            "publisher_url": None,
            "authors_summary": "A Author, F Wang",
            "venue_summary": "CVPR",
            "detail_fields": {"publication date": "2026", "book": "CVPR"},
        }

        original_doi_csl = enrich_record.__globals__["_doi_csl"]
        original_crossref_search = enrich_record.__globals__["_crossref_search"]
        try:
            enrich_record.__globals__["_doi_csl"] = lambda client, doi: None
            enrich_record.__globals__["_crossref_search"] = lambda client, title, max_results: {
                "title": ["Accelerating dataset distillation via model augmentation"],
                "issued": {"date-parts": [[2023]]},
                "container-title": ["CVPR"],
                "type": "proceedings-article",
            }
            enriched = enrich_record(StubClient(), record, 5)
        finally:
            enrich_record.__globals__["_doi_csl"] = original_doi_csl
            enrich_record.__globals__["_crossref_search"] = original_crossref_search

        self.assertEqual(enriched["year"], 2026)
        self.assertEqual(enriched["scholar_year"], 2026)
        self.assertEqual(enriched["publisher_year"], 2023)

    def test_crossref_search_requires_exact_title_match(self):
        class StubClient:
            def get_json(self, url):
                return {
                    "message": {
                        "items": [
                            {"title": ["Completely Different Paper"]},
                            {"title": ["Another Near Miss"]},
                        ]
                    }
                }

        self.assertIsNone(_crossref_search(StubClient(), "Target Paper Title", 5))

    def test_enrich_record_prefers_arxiv_metadata_for_authors_and_title(self):
        class StubClient:
            def get_json(self, url):
                return {}
            def get_text(self, url, headers=None):
                return ""

        record = {
            "title": "Accelerating Large-Scale Dataset Distillation via Exploration-Exploitation Optimization",
            "year": 2026,
            "publisher_url": "https://arxiv.org/abs/2602.15277",
            "authors_summary": "MJ Alahmadi, P Gao, F Wang",
            "venue_summary": "arXiv preprint arXiv:2602.15277",
            "detail_fields": {
                "publication date": "2026/2/17",
                "journal": "arXiv preprint arXiv:2602.15277",
                "description": "arXiv:2602.15277",
            },
        }

        original_crossref_search = enrich_record.__globals__["_crossref_search"]
        original_try_fetch_arxiv_metadata = enrich_record.__globals__["_try_fetch_arxiv_metadata"]
        try:
            enrich_record.__globals__["_crossref_search"] = lambda client, title, max_results: None
            enrich_record.__globals__["_try_fetch_arxiv_metadata"] = lambda client, arxiv_id: {
                "title": "Accelerating Large-Scale Dataset Distillation via Exploration-Exploitation Optimization",
                "authors": ["Alahmadi, Muhammad J.", "Gao, Peng", "Wang, Feiyi", "Xu, Dongkuan"],
                "journal": "arXiv preprint arXiv:2602.15277",
                "url": "https://arxiv.org/abs/2602.15277",
                "arxiv_id": arxiv_id,
            }
            enriched = enrich_record(StubClient(), record, 5)
        finally:
            enrich_record.__globals__["_crossref_search"] = original_crossref_search
            enrich_record.__globals__["_try_fetch_arxiv_metadata"] = original_try_fetch_arxiv_metadata

        self.assertEqual(enriched["title"], record["title"])
        self.assertEqual(enriched["authors"], ["Alahmadi, Muhammad J.", "Gao, Peng", "Wang, Feiyi", "Xu, Dongkuan"])
        self.assertEqual(enriched["arxiv_id"], "2602.15277")

    def test_render_tex_references_expected_bib_sections(self):
        tex = _render_tex(
            {
                "conference": {"bib_name": "conference.bib", "label": "Conference Publications"},
                "journal": {"bib_name": "journal.bib", "label": "Journal Publications"},
                "techreport": {"bib_name": "techreport.bib", "label": "Tech Reports and Preprints"},
            }
        )
        self.assertIn(r"\putbib[conference]", tex)
        self.assertIn(r"\putbib[journal]", tex)
        self.assertIn(r"\putbib[techreport]", tex)

    def test_manual_techreport_is_superseded_by_published_version(self):
        manual_entry = {"category": "techreport", "entry_type": "misc"}
        self.assertTrue(_is_manual_techreport_superseded(manual_entry, "journal"))
        self.assertTrue(_is_manual_techreport_superseded(manual_entry, "conference"))
        self.assertFalse(_is_manual_techreport_superseded(manual_entry, "techreport"))
        self.assertFalse(_is_manual_techreport_superseded({"category": "journal", "entry_type": "article"}, "journal"))

    def test_optional_targets_can_route_to_single_file(self):
        config = {
            "bib_files": {
                "all": {"enabled": True, "path": "/tmp/all.bib"},
                "techreport": {"enabled": False, "path": "/tmp/unused.bib"},
            },
            "routing": {
                "conference": "all",
                "journal": "all",
                "techreport": "all",
                "default": "all",
            },
        }
        self.assertEqual(set(active_bib_files(config)), {"all"})
        self.assertEqual(resolve_routed_category(config, "conference"), "all")
        self.assertEqual(resolve_routed_category(config, "journal"), "all")

    def test_format_text_report_is_email_friendly(self):
        report = {
            "date": "2026-04-18",
            "changed": True,
            "baseline": {"source": "manual_bibs", "cutoff_year": 2025},
            "fetch": {"mode": "authenticated-headless", "row_count": 10, "selected_count": 2},
            "summary": {
                "managed_entries_written": 2,
                "changed_file_count": 1,
                "new_entries": 1,
                "updated_entries": 1,
                "unchanged_agent_entries": 0,
                "manual_existing": 3,
                "possible_duplicates": 0,
                "superseded_manual_techreports": 1,
                "old_entries": 219,
            },
            "files": [
                {"category": "conference", "path": "/tmp/conference.bib", "changed": True, "managed_entry_count": 2}
            ],
            "items": [
                {"title": "Paper A", "category": "conference", "status": "new", "key": "f7b-2026a"},
                {"title": "Paper B", "category": "journal", "status": "updated", "key": "f7b-2026b"},
            ],
        }
        text = _format_text_report(report, 10)
        self.assertIn("Changed: YES", text)
        self.assertIn("Candidate rule: year > 2025", text)
        self.assertIn("Added Or Updated", text)
        self.assertIn("Paper A", text)
        self.assertIn("Paper B", text)

    def test_notification_should_send_only_for_new_or_updated_entries(self):
        self.assertTrue(_notification_should_send({"summary": {"new_entries": 1, "updated_entries": 0}}))
        self.assertTrue(_notification_should_send({"summary": {"new_entries": 0, "updated_entries": 2}}))
        self.assertFalse(_notification_should_send({"summary": {"new_entries": 0, "updated_entries": 0}}))

    def test_build_notification_message_uses_report_recipients(self):
        config = {
            "scholar": {"profile_id": "1JMwC1sAAAAJ"},
            "notifications": {
                "report_from": "fwang2@ornl.gov",
                "report_recipients": ["fwang2@ornl.gov"],
                "subject_prefix": "Bibliography Agent",
            },
            "reporting": {
                "text_report_file": "/tmp/last_update_report.txt",
                "html_report_file": "/tmp/last_update_report.html",
            },
        }
        Path("/tmp/last_update_report.txt").write_text("Hello report\n", encoding="utf-8")
        Path("/tmp/last_update_report.html").write_text("<html><body>Hello html</body></html>\n", encoding="utf-8")
        message = _build_notification_message(config, {"date": "2026-04-18", "summary": {"new_entries": 1, "updated_entries": 0}})
        self.assertEqual(message["To"], "fwang2@ornl.gov")
        self.assertEqual(message["From"], "fwang2@ornl.gov")
        self.assertIn("Bibliography Agent", message["Subject"])
        self.assertEqual(message.get_body(preferencelist=("html",)).get_content_type(), "text/html")

    def test_format_html_report_contains_compact_sections(self):
        report = {
            "date": "2026-04-18",
            "changed": True,
            "fetch": {"mode": "authenticated-headless", "row_count": 10, "selected_count": 2},
            "summary": {
                "changed_file_count": 1,
                "new_entries": 1,
                "updated_entries": 1,
                "possible_duplicates": 0,
            },
            "files": [{"category": "journal", "path": "/Users/f7b/very/long/path/to/journal.bib", "changed": True, "managed_entry_count": 2}],
            "items": [{"title": "Paper A", "status": "new", "category": "journal", "key": "f7b-2026a"}],
        }
        rendered = _format_html_report(report, 10)
        self.assertIn("Bibliography Agent Report", rendered)
        self.assertIn("Paper A", rendered)
        self.assertIn("Added Or Updated", rendered)
        self.assertIn("Changed Files", rendered)
        self.assertIn("~/very/.../to/journal.bib", rendered)


if __name__ == "__main__":
    unittest.main()
