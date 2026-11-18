"""Optional real Spark acceptance using the explicitly fictional examples."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_fine_screening import FIXTURES, ROOT, screening

@unittest.skipUnless(importlib.util.find_spec("pyspark"), "PySpark analysis environment is not installed")
class SparkAcceptance(unittest.TestCase):
    def test_actual_sql_and_saved_outputs_match_the_fictional_run(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            def run(*args):
                result = subprocess.run([sys.executable, "-m", "scripts.data_collect", "fine-screen", *map(str, args)],
                                        cwd=base, text=True, capture_output=True,
                                        env=dict(os.environ, PYTHONPATH=str(ROOT)))
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            batch = base / "run"
            run("prepare", "--stage", "detail", "--input", FIXTURES / "papers.jsonl", "--out", batch,
                "--batch", FIXTURES / "batch.json", "--readers", FIXTURES / "readers.json")
            manifest, rubric, config = screening.load_run(batch)
            for task, paper, record in zip(manifest['tasks'], screening.read_rows(FIXTURES / 'papers.jsonl'), screening.read_rows(FIXTURES / 'detail.jsonl')):
                attempt = batch / 'attempts' / task['task_id'] / '001'
                attempt.mkdir(parents=True)
                screening.save_record(batch, task, paper, record, base, rubric, config, 'detail', attempt)
            out = base / "analysis"
            run("analyze", "--run", batch, "--out", out)
            summary = json.loads((out / "summary.json").read_text())
            self.assertEqual(summary["papers"], 3)
            self.assertEqual(summary["tiers"], {"priority": 1, "possible": 1, "needs_evidence": 1})
            ranked = [json.loads(line) for line in (out / "eligible_ranked.jsonl").read_text().splitlines()]
            self.assertEqual([r["paper_id"] for r in ranked], ["example-priority", "example-possible"])
            self.assertEqual([r["score"] for r in ranked], [24 / 36, 32 / 36])
            pending = [json.loads(line) for line in (out / "needs_evidence.jsonl").read_text().splitlines()]
            self.assertEqual([r["paper_id"] for r in pending], ["example-unknown"])
            self.assertTrue(any((out / "papers.parquet").glob("*.parquet")))


if __name__ == "__main__":
    unittest.main()
