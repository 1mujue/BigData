"""The archived decision remains usable after its original sources are removed."""
from pathlib import Path
import tempfile
import unittest

from test_fine_screening import PREPARE_CONFIG, sample
from scripts.data_collect.fine_screening import screening


class Retention(unittest.TestCase):
    def test_additional_source_keeps_quote_and_identity_without_full_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paper, record = sample()
            source = base / 'input.jsonl'
            screening.write_rows(source, [paper])
            run = base / 'run'
            screening.prepare(source, run, 'detail', **PREPARE_CONFIG)
            manifest, rubric, batch = screening.load_run(run)
            work = base / 'work'
            work.mkdir()
            (work / 'repo.txt').write_text('Relevant implementation paragraph.\nUNUSED_WHOLE_PAGE_CONTENT')
            record['extra_sources'] = [{'source_id':'repo', 'kind':'repository',
                                        'url':'https://example.org/repo', 'path':'repo.txt'}]
            record['evidence'].append({'source_id':'repo','locator':'README',
                                       'quote':'Relevant implementation paragraph.', 'supports':['implementation']})
            attempt = run / 'attempts/000001/001'
            attempt.mkdir(parents=True)
            screening.save_record(run, manifest['tasks'][0], paper, record, work, rubric, batch, 'detail', attempt)
            saved = (run / 'records/000001.json').read_text()
            self.assertNotIn('UNUSED_WHOLE_PAGE_CONTENT', saved)
            self.assertNotIn('repo.txt', saved)
            self.assertIn('Relevant implementation paragraph.', saved)
            self.assertIn('https://example.org/repo', saved)
            self.assertIn('sha256', saved)


if __name__ == '__main__':
    unittest.main()
