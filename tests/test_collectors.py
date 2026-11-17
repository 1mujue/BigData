import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.data_collect.collectors.adapters import Collector
from scripts.data_collect.collectors.model import paper
from scripts.data_collect.collectors.parsers import parse_acl, parse_miniconf
from scripts.data_collect.network import FetchError
from scripts.data_collect.collectors import collect_sources, merge_partitions
from scripts.data_collect.corpus import create_snapshot, load_snapshot, normalize_records


class OfficialCatalogTests(unittest.TestCase):
    def test_cvf_catalog_and_pdf_lookup_use_the_configured_proceedings(self):
        from scripts.data_collect.pdfs.candidates import SourceCatalog

        config = {"adapter": "cvf", "proceedings_url": "https://example.org/{year}/{venue}/"}
        html = '<dl><dt class="ptitle"><a href="papers/example.html">A paper</a></dt></dl>'
        fetch = Mock()
        fetch.get.side_effect = [html, '<meta name="citation_title" content="A paper">']
        collector = Collector(fetch, config, as_of="2030-12-31")
        with patch.object(collector, "miniconf", return_value=[]):
            row, = collector.collect("Example", 2030)

        client = Mock()
        client.text.return_value = html
        pdf = SourceCatalog(client, {"Example": config})
        index = pdf.official_index("Example", 2030)
        self.assertEqual(fetch.get.call_args_list[0].args, ("https://example.org/2030/Example/",))
        client.text.assert_called_once_with("https://example.org/2030/Example/", cache=True)
        self.assertEqual(list(index.values()), [[row["paper_url"]]])

    def test_editorial_type_can_be_excluded_even_without_abstract_or_keywords(self):
        fetch = Mock()
        fetch.json.return_value = {"message": {"total-results": 1, "items": [{"DOI": "10.1234/editorial",
            "title": ["An editorial"], "type": "journal-article", "published": {"date-parts": [[2030, 1, 1]]}}]}}
        fetch.html.return_value = '<meta name="dc.Type" content="editorial">'
        fetch.page_metadata.return_value = {}
        settings = {"issn": "1234-5678", "publisher_url": "https://example.org/{doi}",
                    "pdf_url": "https://example.org/pdf/{doi}", "excluded_publication_types": ["editorial"]}
        collector = Collector(fetch, settings, as_of="2030-11-01")
        self.assertEqual(collector.crossref("Test", 2030), [])
        self.assertEqual(collector.stats["excluded_publication_types"], 1)
        self.assertEqual(collector.stats["unknown_publication_type"], 0)

    def test_partition_refresh_preserves_unrequested_papers_and_sampling_preserves_unseen_papers(self):
        previous = normalize_records([
            {"id": "T:2030:a", "title": "Before", "abstract": "Already obtained", "venue": "T", "year": 2030},
            {"id": "T:2030:b", "title": "Other current paper", "venue": "T", "year": 2030},
            {"id": "T:2029:c", "title": "Previous year", "venue": "T", "year": 2029},
            {"id": "U:2030:d", "title": "Other venue", "venue": "U", "year": 2030}])
        update = [{"id": "T:2030:a", "title": "After", "venue": "T", "year": 2030}]
        complete, _ = merge_partitions(previous, update, {("T", 2030)})
        self.assertEqual({r["title"] for r in complete}, {"After", "Previous year", "Other venue"})
        self.assertEqual(complete[:2], previous[2:])
        self.assertEqual(complete[-1]["paper_id"], previous[0]["paper_id"])
        self.assertEqual(complete[-1]["abstract"], "Already obtained")
        self.assertEqual(complete[-1]["availability"]["abstract"], "present")
        sample, _ = merge_partitions(previous, update, set())
        self.assertEqual(len(sample), 4)
        self.assertEqual({r["title"] for r in sample}, {"After", "Other current paper", "Previous year", "Other venue"})

    def test_failed_partition_retains_prior_records_and_reports_incomplete_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_snapshot([{"id": "T:2030:a", "title": "Existing paper", "venue": "T", "year": 2030}], root / "previous")
            config = root / "sources.json"; config.write_text(json.dumps({"T": {"adapter": "miniconf"}}))
            args = SimpleNamespace(config=config, sources=None, years=[2030], as_of="2030-11-01", workers=1,
                                   limit_per_source=None, output=root / "next", previous=root / "previous", refresh=False)
            with patch("scripts.data_collect.collectors.Fetcher", return_value=SimpleNamespace(counts={})), patch("scripts.data_collect.collectors.Collector") as collector:
                collector.return_value.collect.side_effect = FetchError("example.org", "HTTP 503")
                result = collect_sources(args)
            self.assertEqual(result["collection_status"], "partial")
            self.assertEqual(load_snapshot(root / "next")[1], load_snapshot(root / "previous")[1])
            partition = result["partitions"][0]
            self.assertEqual((partition["status"], partition["count"], partition["carried_count"]), ("retained_after_failure", 1, 1))

    def test_missing_optional_abstract_file_uses_embedded_official_abstract(self):
        fetch = Mock()
        fetch.get.return_value = '<script src="orals-posters.json"></script><script src="abstracts.json"></script>'
        fetch.json.side_effect = [[{"id": 1, "name": "Official paper", "eventtype": "Poster", "abstract": "Embedded abstract"}],
                                  FetchError("example.org", "HTTP 404")]
        collector = Collector(fetch, {"catalog_url": "https://example.org/{year}/papers", "event_types": ["Poster"]}, as_of="2030-11-01")
        rows = collector.miniconf("Test", 2030)
        self.assertEqual(rows[0]["abstract"], "Embedded abstract")

    def test_anthology_volume_selection_is_task_configuration(self):
        xml = '<collection><volume id="long"><paper id="1"><title>Long paper</title></paper></volume><volume id="short"><paper id="2"><title>Short paper</title></paper></volume></collection>'
        rows = parse_acl(xml, 2030, "https://example.org/catalog", "ACL", "2030.acl", ["short"])
        self.assertEqual([r["title"] for r in rows], ["Short paper"])
        self.assertEqual(rows[0]["id"], "ACL:2030:2030.acl-short.2")
        self.assertIsNone(rows[0]["doi"])

    def test_official_track_selection_can_change_without_code_changes(self):
        events = [{"id": i, "name": title, "sourceurl": origin, "eventtype": "Poster"}
                  for i, title, origin in [(1, "Main paper", "ICML.cc/2030/Conference"),
                                           (2, "Workshop paper", "ICML.cc/2030/Workshop")]]
        settings = {"event_types": ["Poster"], "source_contains": "ICML.cc/{year}/Conference"}
        rows, excluded = parse_miniconf(events, {}, "ICML", 2030, "https://example.org/list", settings)
        self.assertEqual([r["title"] for r in rows], ["Main paper"])
        self.assertEqual(excluded, 1)
        settings["source_contains"] = "ICML.cc/{year}/Workshop"
        rows, _ = parse_miniconf(events, {}, "ICML", 2030, "https://example.org/list", settings)
        self.assertEqual([r["title"] for r in rows], ["Workshop paper"])

    def test_publication_enrichment_keeps_unmatched_official_acceptances(self):
        collector = Collector(Mock(), {"pmlr_volumes": {"2030": 999}}, as_of="2030-11-01")
        accepted = paper("ICML", 2030, "official-a", "Accepted without DOI", "https://example.org/list")
        accepted.update(abstract="Available abstract", pdf_urls=["https://example.org/a.pdf"])
        unrelated = paper("ICML", 2030, "published-b", "Other proceedings paper", "https://example.org/proceedings")
        with patch.object(collector, "miniconf", return_value=[accepted]), patch.object(collector, "pmlr", return_value=[unrelated]), patch.object(collector, "paperlists"):
            result = collector.icml("ICML", 2030)
        self.assertEqual([r["id"] for r in result], [accepted["id"]])
        self.assertIsNone(result[0]["doi"])

    def test_supplement_cannot_add_or_remove_official_candidates(self):
        fetch = Mock()
        fetch.github.return_value = json.dumps([
            {"title": "Official candidate", "status": "reject"},
            {"title": "Supplement only", "status": "accept", "abstract": "Extra"}])
        collector = Collector(fetch, {"paperlists": {"repository": "example/papers", "path": "{year}.json"}}, as_of="2030-11-01")
        row = paper("Test", 2030, "official", "Official candidate", "https://example.org")
        self.assertEqual(collector.paperlists("Test", 2030, [row]), [row])


class SelectedPdfTests(unittest.TestCase):
    def test_later_selection_reuses_admitted_pdf_and_keeps_original_run(self):
        from scripts.data_collect.pdfs.ingest import collect_selected_pdfs
        from scripts.data_collect.tool import read_jsonl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "store.json").write_text(json.dumps({"kind": "selected-pdf-store", "hdfs_root": "/papers/test"}))
            config = root / "sources.json"; config.write_text("{}")
            state = {"paper_id": "a" * 64, "status": "stored", "venue": "Test", "year": 2030,
                     "hdfs_path": "/papers/test/pdfs/a.pdf", "bytes": 123, "source": "official", "first_screening_run": "first"}
            (root / "downloads.jsonl").write_text(json.dumps(state) + "\n")
            args = SimpleNamespace(selection=root / "selection", config=config, output=root, hdfs_root="/papers/test",
                                   workers=1, venue_workers=1, max_searches=0)
            selection = {"run_id": "second", "selection_sha256": "fixture"}
            rows = [{"paper_id": state["paper_id"], "venue": "Test", "year": 2030}]
            with patch("scripts.data_collect.pdfs.ingest.load_selection", return_value=(selection, rows)), patch("scripts.data_collect.pdfs.ingest.HdfsStore") as store, patch("scripts.data_collect.pdfs.ingest.PdfResolver") as resolver, patch("scripts.data_collect.pdfs.ingest.spark_session"):
                store.return_value.sizes.return_value = {state["hdfs_path"]: 123}
                result = collect_selected_pdfs(args)
                resolver.return_value.download.assert_not_called()
            self.assertEqual(result["statuses"], {"stored": 1})
            self.assertEqual(read_jsonl(root / "runs/second/manifest.jsonl")[0]["first_screening_run"], "first")


if __name__ == "__main__":
    unittest.main()
