import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock, patch

from scripts.data_collect.collectors.dblp import DblpCollector


def binding(**values):
    return {key: {"value": str(value)} for key, value in values.items()}


class CollectionTests(unittest.TestCase):
    def test_partial_collection_exits_nonzero_before_a_shell_pipeline_can_continue(self):
        from scripts.data_collect import __main__ as cli
        with patch("dotenv.load_dotenv"), patch.object(cli, "collect", return_value={"collection_status": "partial"}), patch("sys.argv", ["collector", "collect", "--config", "sources.json", "--years", "2030", "--output", "snapshot"]):
            with self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 1)

    def test_metadata_cli_requires_configured_sources_and_explicit_years(self):
        from scripts.data_collect import __main__ as cli
        with patch("dotenv.load_dotenv"), patch.object(cli, "collect", return_value=None) as handler:
            for options in (["--config", "venues.json"], ["--years", "2024"]):
                with self.subTest(options=options), patch("sys.argv", ["collector", "collect", "--output", "snapshot", *options]), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:cli.main()
                    self.assertEqual(error.exception.code, 2)
            handler.assert_not_called()
            with patch("sys.argv", ["collector", "collect", "--config", "venues.json", "--years", "2024", "--output", "snapshot"]):
                cli.main()
            args=handler.call_args.args[0]
            self.assertEqual((args.config,args.years,args.sources),(Path("venues.json"),[2024],None))

    def test_pdf_store_keeps_its_root_across_different_selections(self):
        from scripts.data_collect.pdfs.ingest import collect_selected_pdfs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);run=root/"run";run.mkdir()
            saved=json.dumps({"kind":"selected-pdf-store","hdfs_root":"/papers/test"}).encode()
            (run/"store.json").write_bytes(saved)
            config=root/"venues.json";config.write_text('{}')
            args=SimpleNamespace(selection=root/"selection",config=config,output=run,hdfs_root="/papers/test")
            for run_id in ('first','second'):
                with patch("scripts.data_collect.pdfs.ingest.load_selection",return_value=({"run_id":run_id},[{"paper_id":"a"*64}])), patch("scripts.data_collect.pdfs.ingest.HdfsStore") as store:
                    store.return_value.prepare.side_effect=RuntimeError("storage boundary")
                    with self.assertRaisesRegex(RuntimeError,"storage boundary"):collect_selected_pdfs(args)
                    self.assertEqual((run/"store.json").read_bytes(),saved)
                    args.hdfs_root="/papers/other"
                    with self.assertRaisesRegex(ValueError,"different PDF store"):collect_selected_pdfs(args)
                    args.hdfs_root="/papers/test"

    def test_forbidden_urls_do_not_block_later_requests_on_same_host(self):
        from scripts.data_collect.network import FetchError, HttpClient

        replies = iter([403, 403, 403, 200])

        def respond(command, **kwargs):
            status = next(replies)
            Path(command[command.index("--output") + 1]).write_text("response")
            return SimpleNamespace(returncode=0, stdout=f"{status}\nhttps://example.org/paper.pdf")

        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.data_collect.network.subprocess.run", side_effect=respond) as request, \
                patch("scripts.data_collect.network.time.sleep"):
            client = HttpClient()
            for index in range(3):
                with self.assertRaisesRegex(FetchError, "HTTP 403"):
                    client.fetch(f"https://example.org/private/{index}", Path(directory) / "response")
            self.assertEqual(client.fetch("https://example.org/paper.pdf", Path(directory) / "response"),
                             "https://example.org/paper.pdf")
            self.assertEqual(request.call_count, 4)

    def test_openreview_links_remain_after_official_archive_candidates(self):
        from scripts.data_collect.pdfs.candidates import SourceCatalog, normalized

        catalog = SourceCatalog(Mock(), {})
        official = "https://proceedings.iclr.cc/paper/example"
        openreview = "https://openreview.net/forum?id=example"
        paper = {"venue": "ICLR", "year": 2024, "title": "Our paper", "document_urls": [openreview]}
        catalog.indexes[("ICLR", 2024)] = {normalized(paper["title"]): [official]}
        self.assertEqual([entry["url"] for entry in catalog.direct(paper)], [official, openreview])
        catalog.indexes.clear()
        self.assertEqual([entry["url"] for entry in catalog.direct(paper)], [openreview])

    def test_figshare_identification_and_api_failure_preserve_pdf_fallback(self):
        import pymupdf
        from scripts.data_collect.network import FetchError
        from scripts.data_collect.pdfs.download import PdfResolver
        from scripts.data_collect.pdfs.candidates import candidate

        paper = {"paper_id": "a" * 64, "title": "Our paper", "authors": ["Alice Smith"]}
        page = "https://repository.example.org/articles/journal_contribution/paper/123"
        pdf = "https://repository.example.org/paper.pdf"
        marker = '<script src="https://static.figshare.com/app.js"></script>'
        for name, html, response in [
            ("unrelated article path", f'<meta name="citation_pdf_url" content="{pdf}">', None),
            ("Figshare API failure", marker + f'<meta name="citation_pdf_url" content="{pdf}">',
             FetchError("api.figshare.com", "HTTP 503")),
            ("institutional Figshare", marker, {
                "title": paper["title"], "authors": [{"full_name": "Alice Smith"}],
                "files": [{"name": "paper.pdf", "download_url": pdf}],
            }),
        ]:
            def fetch(url, path):
                if url == page:
                    path.write_text(html)
                else:
                    with pymupdf.open() as document:
                        document.new_page().insert_text((40, 40), "Our paper\nAlice Smith")
                        document.save(path)
                return url

            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                client = Mock()
                client.fetch.side_effect = fetch
                client.json.side_effect = response if isinstance(response, Exception) else None
                client.json.return_value = response or {}
                result = PdfResolver(client, Path(directory)).download(paper, [candidate(page, "official")])
                self.assertEqual(result["status"], "downloaded")
                self.assertEqual(result["source_url"], pdf)
                if response is None:
                    client.json.assert_not_called()
                else:
                    client.json.assert_called_once_with("https://api.figshare.com/v2/articles/123")
                if isinstance(response, Exception):
                    self.assertIn("HTTP 503", result["attempts"][0]["error"])

    def test_rate_limit_waits_for_retry_after_before_retrying(self):
        from scripts.data_collect.network import HttpClient

        replies = iter([429, 200])

        def respond(command, **kwargs):
            status = next(replies)
            if "--dump-header" in command:
                headers = Path(command[command.index("--dump-header") + 1])
                headers.write_text(f"HTTP/2 {status}\r\nRetry-After: 11\r\n\r\n")
            Path(command[command.index("--output") + 1]).write_text("response")
            return SimpleNamespace(returncode=0, stdout=f"{status}\nhttps://api.firecrawl.dev/v2/search")

        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.data_collect.network.subprocess.run", side_effect=respond) as request, \
                patch("scripts.data_collect.network.time.sleep") as sleep:
            HttpClient().fetch("https://api.firecrawl.dev/v2/search", Path(directory) / "response")
            self.assertEqual(request.call_count, 2)
            sleep.assert_any_call(11)

    def test_search_failure_does_not_disable_other_papers(self):
        from scripts.data_collect.pdfs.ingest import collect_selected_pdfs, read_jsonl
        from scripts.data_collect.network import FetchError

        cached = set()

        def search(paper):
            if paper["title"] == "First paper":
                raise FetchError("api.firecrawl.dev", "HTTP 503")
            cached.add(paper["paper_id"])
            return []

        with tempfile.TemporaryDirectory() as directory, patch.multiple(
            "scripts.data_collect.pdfs.ingest", load_selection=DEFAULT, HdfsStore=DEFAULT,
            SourceCatalog=DEFAULT, PdfResolver=DEFAULT, OpenAlexPdfResolver=DEFAULT,
            FirecrawlSearch=DEFAULT, spark_session=DEFAULT,
        ) as mocks:
            root = Path(directory)
            config = root / "venues.json"
            config.write_text(json.dumps({"Test": {"stream": "conf/test"}}))
            args = SimpleNamespace(config=config, selection=root / "selection",
                                   limit_per_venue=None, output=root / "output",
                                   hdfs_root="/papers/test", workers=1, venue_workers=1,
                                   max_searches=None)
            mocks["load_selection"].return_value = ({"run_id":"test-run","selection_sha256":"test"}, [
                {"paper_id": "a"*64, "title": "First paper", "venue":"Test", "year": 2024},
                {"paper_id": "b"*64, "title": "Second paper", "venue":"Test", "year": 2024},
            ])
            mocks["HdfsStore"].return_value.sizes.return_value = {}
            mocks["SourceCatalog"].return_value.direct.return_value = []
            mocks["OpenAlexPdfResolver"].return_value.resolve.return_value = {}
            mocks["PdfResolver"].return_value.download.side_effect = lambda paper, candidates: {
                "paper_id": paper["paper_id"], "status": "missing", "attempts": []}
            service = mocks["FirecrawlSearch"].return_value
            service.cached.side_effect = lambda paper: paper["paper_id"] in cached
            service.search.side_effect = search

            collect_selected_pdfs(args)

            searched = [call.args[0] for call in service.search.call_args_list]
            self.assertEqual([paper["title"] for paper in searched], ["First paper", "Second paper"])
            final = {row["paper_id"]: row for row in read_jsonl(args.output / "runs/test-run/manifest.jsonl")}
            self.assertEqual(final[searched[0]["paper_id"]]["reason"], "search_service_failed")
            self.assertEqual(final[searched[1]["paper_id"]]["reason"], "no_downloadable_candidate")

    def test_limit_counts_papers_and_keeps_all_authors_and_links(self):
        rows = [binding(paper="https://dblp.org/rec/conf/x/p1", title="Paper", year=2024,
                        author=author, link=link, kind="https://dblp.org/rdf/schema#Inproceedings")
                for author in ["Alice", "Bob"] for link in ["https://a/p", "https://b/p.pdf"]]
        with patch.object(DblpCollector, "_fetch_page", return_value=rows):
            result = DblpCollector("conf/x", 2024).collect(1, 100)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["authors"], ["Alice", "Bob"])
        self.assertEqual(result[0]["document_urls"], ["https://a/p", "https://b/p.pdf"])

    def test_pdf_fallback_rejects_a_different_paper(self):
        import pymupdf
        from scripts.data_collect.pdfs.download import PdfDownloader, PdfResolver

        class Client:
            def fetch(self, url, path, **kwargs):
                if url.endswith("/article"):
                    path.write_text('<a href="reference.pdf">PDF</a>')
                else:
                    text = "Another paper\nSomeone Else" if url.endswith("reference.pdf") else "Our paper\nAlice Smith"
                    with pymupdf.open() as doc:
                        doc.new_page().insert_text((40, 40), text)
                        doc.save(path)
                return url

        paper = {"paper_id": "a" * 64, "title": "Our paper", "authors": ["Alice Smith"]}
        candidates = [{"url": url, "source": "official", "trusted": True} for url in
                      ["https://example.org/article", "https://example.org/paper.pdf"]]
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            result = PdfResolver(client, Path(directory)).download(paper, candidates)
            self.assertEqual(result["status"], "downloaded")
            self.assertEqual(result["source_url"], candidates[1]["url"])
            self.assertEqual([entry["status"] for entry in result["attempts"]], ["not_pdf", "failed", "pdf"])
            with patch("scripts.data_collect.pdfs.download.HttpClient", return_value=client):
                path, pages = PdfDownloader(Path(directory)).download("b" * 64, candidates[1]["url"])
            self.assertTrue(path.is_file())
            self.assertEqual(pages, 1)

    def test_researchr_badges_are_not_part_of_the_paper_title(self):
        from scripts.data_collect.pdfs.candidates import SourceCatalog
        client = Mock()
        client.text.return_value = '''
          <div id="event-overview"><table><tr>
            <td><a data-event-modal="event-1">A useful paper<span class="output-badge">Security</span></a></td>
          </tr></table></div>
          <div id="event-modal-loader"><form action="/detail">
            <input class="event-id-input" name="event"><button submitid="load">Load</button>
          </form></div>'''
        client.json.return_value = [{"action": "append", "id": "event-modals", "value":
                                    '<a class="publication-link" href="https://authors.org/p.pdf">Pre-print</a>'}]
        catalog = SourceCatalog(client, {"ICSE": {"researchr": {"2025": "https://conf.researchr.org/track/x"}}})
        catalog.prepare_researchr("ICSE", 2025)
        links = catalog.researchr({"venue": "ICSE", "year": 2025, "title": "A useful paper."})
        self.assertEqual([entry["url"] for entry in links], ["https://authors.org/p.pdf"])

    def test_resume_skips_confirmed_files_but_requeues_missing_hdfs_files(self):
        from scripts.data_collect.pdfs.ingest import resume_states
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "downloads.jsonl"
            records = [
                {"paper_id": "a", "status": "missing"},
                {"paper_id": "a", "status": "stored", "hdfs_path": "/papers/a.pdf", "bytes": 100},
                {"paper_id": "b", "status": "stored", "hdfs_path": "/papers/b.pdf", "bytes": 200},
                {"paper_id": "c", "status": "missing", "attempts": [{"url": "https://example.org/failed"}]},
            ]
            journal.write_text("\n".join(json.dumps(record) for record in records))
            states = resume_states(journal, {"/papers/a.pdf": 100})
            self.assertEqual(states["a"]["status"], "stored")
            self.assertNotIn("b", states)
            self.assertNotIn("c", states)


if __name__ == "__main__":
    unittest.main()
