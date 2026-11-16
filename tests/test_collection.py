import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.data_collect.metadata import DblpCollector


def binding(**values):
    return {key: {"value": str(value)} for key, value in values.items()}


class CollectionTests(unittest.TestCase):
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
        from scripts.data_collect.pdf import PdfDownloader, PdfResolver

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
            with patch("scripts.data_collect.pdf.HttpClient", return_value=client):
                path, pages = PdfDownloader(Path(directory)).download("b" * 64, candidates[1]["url"])
            self.assertTrue(path.is_file())
            self.assertEqual(pages, 1)

    def test_researchr_badges_are_not_part_of_the_paper_title(self):
        from scripts.data_collect.sources import SourceCatalog
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
        from scripts.data_collect.batch import resume_states
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
