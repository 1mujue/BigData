import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from scripts.data_collect.storage import HdfsStore
from scripts.data_collect.tool import file_sha256, read_jsonl, write_json, write_jsonl
from scripts.data_collect.fine_screening.prepare_inputs import prepare_inputs


class ScreeningInputs(unittest.TestCase):
    def test_coarse_selection_joins_pdfs_by_id_and_keeps_missing_papers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            coarse, pdf_run, output = base / "coarse", base / "downloads", base / "papers.jsonl"
            rows = [
                {
                    "paper_id": pid, "title": title, "venue": "Example", "year": 2025,
                    "paper_url": None, "pdf_urls": [], "abstract": None,
                    "tags": [{"value": "evaluation", "kind": "keyword", "source": "catalog"}],
                    "scope": {"status": status}, "retrieval": {"run_id": "selection"},
                }
                for pid, title, status in [
                    ("one", "A published paper", "published"), ("two", "An accepted paper", "accepted")
                ]
            ]
            write_jsonl(coarse / "selected.jsonl", rows)
            write_json(coarse / "configuration.json", {})
            pick_hash = file_sha256(coarse / "selected.jsonl")
            write_json(coarse / "summary.json", {
                "kind": "screening-selection", "status": "complete", "run_id": "selection",
                "selected_count": 2, "selection_sha256": pick_hash,
                "configuration_sha256": file_sha256(coarse / "configuration.json"),
            })
            with pymupdf.open() as document:
                document.new_page().insert_text((50, 50), "An experiment from the original paper.")
                body = document.tobytes()
            pdf_summary = {"screening_run": "selection", "selection_sha256": pick_hash}
            write_json(pdf_run / "summary.json", pdf_summary)
            write_jsonl(pdf_run / "manifest.jsonl", [
                {"paper_id": "two", "status": "missing", "reason": "no PDF"},
                {"paper_id": "one", "status": "stored", "hdfs_path": "/papers/test/one.pdf",
                 "source_url": "https://example.org/one.pdf", "sha256": hashlib.sha256(body).hexdigest()},
            ])
            with patch.object(HdfsStore, "dfs", return_value=body) as read:
                result = prepare_inputs(coarse, pdf_run, output)
            read.assert_called_once_with("-cat", "/papers/test/one.pdf", decode=False)
            published, pending = read_jsonl(output)
            self.assertEqual((published["paper_id"], pending["paper_id"]), ("one", "two"))
            self.assertEqual(published["tags"], ["evaluation"])
            self.assertTrue(published["source_eligible"])
            self.assertIn("[PDF page 1]\nAn experiment", published["sources"][0]["text"])
            self.assertEqual(published["sources"][0]["url"], "https://example.org/one.pdf")
            self.assertIsNone(pending["source_eligible"])
            self.assertEqual(pending["sources"], [])
            self.assertEqual(result["missing_text"], [{"paper_id": "two", "reason": "no PDF"}])
            write_json(pdf_run / "summary.json", pdf_summary | {"selection_sha256": "another selection"})
            with self.assertRaisesRegex(ValueError, "different coarse selection"):
                prepare_inputs(coarse, pdf_run, base / "wrong.jsonl")
