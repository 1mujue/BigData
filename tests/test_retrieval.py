import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from scripts.data_collect.corpus import create_snapshot, load_snapshot, normalize_records
from scripts.data_collect.retrieval.embeddings import EmbeddingCache, document_text, query_text, vector_signature
from scripts.data_collect.retrieval.screening import fuse_scores, index_corpus, load_selection, paper_mapping, rank_topic, search, store_run
from scripts.data_collect.tool import file_sha256, write_json, write_jsonl


CONFIG = {"endpoint": "https://example.invalid/embeddings", "model": "fixture-vectors", "dimensions": 2}


class RetrievalTests(unittest.TestCase):
    def test_added_publication_identifier_does_not_rekey_a_known_paper(self):
        previous = normalize_records([{"id": "T:2027:forum", "title": "Original title", "venue": "T", "year": 2027,
            "openreview_id": "forum", "abstract": "Existing abstract", "field_sources": {"abstract": ["https://example.org"]}}])
        updated = normalize_records([{"id": "T:2027:publisher", "title": "Updated title", "venue": "T", "year": 2027,
            "openreview_id": "forum", "doi": "10.1234/example", "abstract": None}], previous)
        self.assertEqual(updated[0]["paper_id"], previous[0]["paper_id"])
        self.assertEqual(updated[0]["source_key"], previous[0]["source_key"])
        self.assertEqual(updated[0]["abstract"], "Existing abstract")
        self.assertEqual(updated[0]["field_sources"]["abstract"], ["https://example.org"])

    def test_snapshot_content_is_checked_and_same_titles_keep_separate_identities(self):
        rows = [{"id": f"T:2030:{i}", "title": "Same title", "year": 2030, "venue": "T"} for i in range(2)]
        with tempfile.TemporaryDirectory() as directory:
            create_snapshot(rows, directory)
            _, saved = load_snapshot(directory)
            self.assertEqual(len({r["paper_id"] for r in saved}), 2)
            with (Path(directory) / "papers.jsonl").open("a") as f:
                f.write("\n")
            with self.assertRaisesRegex(ValueError, "manifest"):
                load_snapshot(directory)

    def test_rrf_includes_vector_only_papers_and_has_explicit_tie_order(self):
        result = fuse_scores({"a": 10, "b": 5}, {"a": .8, "b": .9, "c": .1}, 60)
        self.assertEqual([r["paper_id"] for r in result], ["b", "a", "c"])
        self.assertAlmostEqual(result[0]["rrf_score"], 1 / 61 + 1 / 62)
        self.assertAlmostEqual(result[2]["rrf_score"], 1 / 63)
        self.assertIsNone(result[2]["keyword_rank"])

    def test_cache_only_requests_changed_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = EmbeddingCache(Path(directory) / "vectors.db", CONFIG)
            encode = Mock(side_effect=lambda texts: (np.asarray([[1., 0.] for _ in texts]), len(texts)))
            try:
                cache.get_or_create(["unchanged", "before"], encode)
                cache.get_or_create(["unchanged", "before"], encode)
                cache.get_or_create(["unchanged", "after"], encode)
                self.assertEqual([c.args[0] for c in encode.call_args_list], [["unchanged", "before"], ["after"]])
            finally:
                cache.close()

    def test_pdf_admission_rejects_corpus_and_altered_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / "summary.json", {"kind": "paper-corpus", "status": "complete"})
            with self.assertRaisesRegex(ValueError, "screening"):
                load_selection(root)
            write_jsonl(root / "selected.jsonl", [{"paper_id": "a" * 64, "retrieval": {"run_id": "run"}}])
            write_json(root / "configuration.json", {"method": "test"})
            write_json(root / "summary.json", {"kind": "screening-selection", "status": "complete", "run_id": "run",
                "selected_count": 1, "selection_sha256": file_sha256(root / "selected.jsonl"),
                "configuration_sha256": file_sha256(root / "configuration.json")})
            self.assertEqual(len(load_selection(root)[1]), 1)
            (root / "selected.jsonl").write_text("{}\n")
            with self.assertRaisesRegex(ValueError, "content"):
                load_selection(root)

    def test_pdf_cli_requires_a_screening_result(self):
        from scripts.data_collect import __main__ as cli
        with patch("dotenv.load_dotenv"), patch.object(cli, "collect_pdfs") as handler:
            handler.return_value = None
            with patch("sys.argv", ["collector", "collect-pdfs"]), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)
            with patch("sys.argv", ["collector", "collect-pdfs", "--selection", "selected-run"]):
                cli.main()
            self.assertEqual(handler.call_args.args[0].selection, Path("selected-run"))

    def test_failed_run_registration_does_not_publish_a_complete_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch("scripts.data_collect.storage.HdfsStore"), patch("scripts.data_collect.tool.spark_session"):
            output = Path(directory)
            es = Mock()
            es.exists.return_value = True
            es.request.side_effect = RuntimeError("Run registration failed")
            summary = {"run_id": "example", "snapshot_id": "snapshot"}
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                store_run(output, summary, [], es, "/papers/test")
            self.assertFalse((output / "summary.json").exists())


@unittest.skipUnless(os.environ.get("BIGDATA_ES_TESTS") == "1", "Set BIGDATA_ES_TESTS=1 for real ES checks")
class ElasticsearchTests(unittest.TestCase):
    def test_incomplete_index_is_recreated_without_deleted_document_statistics(self):
        from scripts.data_collect.retrieval.elastic import Elasticsearch
        es = Elasticsearch()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = create_snapshot([{"id": "fixture:" + uuid.uuid4().hex, "title": "Correct title",
                                         "abstract": "Actual abstract", "venue": "Test", "year": 2030}], root / "corpus")
            _, rows = load_snapshot(root / "corpus")
            signature = vector_signature(CONFIG)
            index = f'papers-{manifest["snapshot_id"][:16]}-{signature[:12]}'
            cache_path = root / "cache.db"
            cache = EmbeddingCache(cache_path, CONFIG)
            cache.get_or_create([document_text(rows[0])], lambda _: (np.array([[1., 0.]]), 0))
            cache.close()
            meta = {"kind": "paper-corpus", "snapshot_id": manifest["snapshot_id"],
                    "vector_signature": signature, "ready": False}
            try:
                es.request("PUT", "/" + index, paper_mapping(2, meta))
                es.bulk(index, [(rows[0]["paper_id"], {**rows[0], "title": "Interrupted previous build", "embedding": [0., 1.]})])
                with patch("scripts.data_collect.storage.HdfsStore"), patch.object(Elasticsearch, "point_alias"), patch("scripts.data_collect.retrieval.embeddings.EmbeddingAPI.encode", side_effect=AssertionError("Cache must be used")):
                    result = index_corpus(root / "corpus", {"embedding": CONFIG}, cache_path)
                self.assertEqual(result["count"], 1)
                self.assertEqual(es.documents(index, [rows[0]["paper_id"]])[0]["title"], "Correct title")
                stats = es.request("GET", f"/{index}/_stats/docs")
                self.assertEqual(stats["_all"]["primaries"]["docs"]["deleted"], 0)
            finally:
                if es.exists(index):es.request("DELETE", "/" + index)

    def test_exact_search_covers_corpus_and_filters_a_prior_selection_without_rewriting_it(self):
        from scripts.data_collect.retrieval.elastic import Elasticsearch
        es = Elasticsearch()
        suffix = uuid.uuid4().hex
        index, runs = "papers-test-" + suffix, "paper-runs-test-" + suffix
        meta = {"kind": "paper-corpus", "ready": True, "snapshot_id": suffix,
                "embedding": CONFIG, "vector_signature": vector_signature(CONFIG), "count": 3}
        rows = normalize_records([
            {"id": "T:a", "title": "Program repair with tests", "abstract": "Execution feedback repairs code", "venue": "T", "year": 2030},
            {"id": "T:b", "title": "Image reconstruction", "abstract": "Visual representation models", "venue": "T", "year": 2030},
            {"id": "T:c", "title": "Program repair with tests", "abstract": "Static patches for compilers", "venue": "T", "year": 2030},
        ])
        embeddings = [[1., 0.], [0., 1.], [.6, .8]]
        profile = {"embedding": CONFIG, "rrf_k": 60, "fields": ["title^6", "abstract^2"]}
        try:
            es.request("PUT", "/" + index, paper_mapping(2, meta))
            es.bulk(index, ((r["paper_id"], {**r, "embedding": v}) for r, v in zip(rows, embeddings)))
            ranked = rank_topic(es, index, {"queries": ["program repair"]}, np.array([[1., 0.]]), profile, [])
            values = {r["paper_id"]: r for r in ranked}
            self.assertEqual(len(ranked), 3)
            for row, expected in zip(rows, [1., 0., .6]):
                self.assertAlmostEqual(values[row["paper_id"]]["vector_similarity"], expected, places=5)
            self.assertEqual(ranked[0]["paper_id"], rows[0]["paper_id"])
            self.assertEqual(values[rows[1]["paper_id"]]["keyword_score"], 0)
            es.request("PUT", "/" + runs, {"mappings": {"dynamic": False}})
            historical = {"status": "complete", "index": index, "selected_ids": [rows[1]["paper_id"]], "original_score": .123}
            es.request("PUT", f"/{runs}/_doc/example?refresh=true", historical)
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cache.db"
                cache = EmbeddingCache(path, CONFIG)
                cache.get_or_create([query_text("program repair", CONFIG)], lambda _: (np.array([[1., 0.]]), 0))
                cache.close()
                with patch("scripts.data_collect.retrieval.screening.RUN_INDEX", runs), patch("scripts.data_collect.retrieval.embeddings.EmbeddingAPI.encode", side_effect=AssertionError("Cache must be used")):
                    result = search(index, "program repair", profile, path, run_id="example")
                self.assertEqual([r["paper_id"] for r in result["results"]], [rows[1]["paper_id"]])
                self.assertEqual(es.request("GET", f"/{runs}/_doc/example")["_source"], historical)
        finally:
            for name in (index, runs):
                if es.exists(name):
                    es.request("DELETE", "/" + name)
