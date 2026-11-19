"""Exercise the real runner CLI with a deterministic replacement executable."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

from test_fine_screening import FIXTURES, ROOT, sample
from scripts.data_collect.fine_screening import screening


class Execution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.auth = self.base / 'auth.json'
        self.auth.write_text('{"test_credential":true}')
        self.codex = self.base / 'codex'
        pairs = [sample((2, 2, 2, 2), name) for name in ['good', 'bad', 'invalid', 'slow']]
        self.records = {p['paper_id']: r for p, r in pairs}
        self.papers = {p['paper_id']: p for p, r in pairs}
        self.reader_settings = screening.load_json(FIXTURES / 'readers.json')
        self.reader_settings['codex']['model'] = 'fixture-reader'
        self.readers = self.base / 'readers.json'
        screening.write_json(self.readers, self.reader_settings)
        self.codex.write_text('#!' + sys.executable + '\n' + '''
import json, os, pathlib, shlex, subprocess, sys, time
if '--version' in sys.argv:
    print('codex-cli fixture'); sys.exit(0)
work = pathlib.Path(sys.argv[sys.argv.index('-C') + 1])
paper = json.loads((work / 'input.json').read_text())
pid = paper['paper_id']
assert pathlib.Path(os.environ['HOME']).is_relative_to(work.parent)
assert 'PERSONAL_SECRET_TEST' not in os.environ
assert '--ephemeral' in sys.argv and '--ignore-user-config' in sys.argv
assert 'agents.enabled=false' in sys.argv
print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'read synthetic paper','aggregated_output':paper['sources'][0]['text']}}), flush=True)
(pathlib.Path(os.environ['CODEX_HOME']) / 'disposable-cache').write_text('delete me')
time.sleep(60 if pid == 'slow' else 0.2)
if pid == 'bad':
    print('synthetic failure', file=sys.stderr); sys.exit(3)
records = ''' + repr(self.records) + '''
(work / 'results.jsonl').write_text('{broken' if pid == 'invalid' else json.dumps(records[pid]) + '\\n')
if pid == 'good':
    assignment = json.loads((work / 'assignment.json').read_text())
    subprocess.run(shlex.split(assignment['check_command']), cwd=work, stdout=subprocess.DEVNULL, check=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':100,'output_tokens':10}}), flush=True)
''')
        self.codex.chmod(0o755)

    def prepare(self, names):
        source = self.base / 'papers.jsonl'
        screening.write_rows(source, [self.papers[n] for n in names])
        run = self.base / 'run'
        screening.prepare(source, run, 'detail', readers_path=self.readers, batch_path=FIXTURES / 'batch.json')
        return source, run

    def command(self, run, timeout='5'):
        return [sys.executable, '-m', 'scripts.data_collect', 'fine-screen', 'run', '--run', str(run),
                '--auth-file', str(self.auth), '--codex', str(self.codex),
                '--runtime', str(self.base / 'runtime'), '--timeout', timeout]

    def call(self, run, timeout='5'):
        env = dict(os.environ, PERSONAL_SECRET_TEST='must not reach worker')
        return subprocess.run(self.command(run, timeout), cwd=ROOT, text=True, capture_output=True, env=env)

    def assert_clean(self):
        self.assertEqual(list((self.base / 'runtime').iterdir()), [])

    def test_success_is_usable_after_cleanup_and_input_removal(self):
        source, run = self.prepare(['good'])
        result = self.call(run)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assert_clean()
        source.unlink()
        rows, report = screening.complete_records(run)
        self.assertEqual(report['valid'], 1)
        self.assertAlmostEqual(rows[0]['score'], 2 / 3)
        self.assertEqual(rows[0]['assessment']['model'], 'fixture-reader')
        self.assertIn('read synthetic paper', next(run.glob('attempts/*/*/trace.jsonl')).read_text())
        self.assertFalse(list(run.rglob('auth.json')))
        self.assertFalse(list(run.rglob('input.json')))

    def test_failure_invalid_output_and_timeout_leave_only_records_and_trace(self):
        _, run = self.prepare(['bad', 'invalid', 'slow'])
        result = self.call(run, '0.6')
        self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
        self.assert_clean()
        rows, report = screening.collect_records(run)
        self.assertEqual(rows, [])
        statuses = [screening.load_json(p)['status'] for p in run.glob('attempts/*/*/status.json')]
        self.assertCountEqual(statuses, ['failed', 'failed', 'timed_out'])
        self.assertEqual(len(report['missing']), 3)
        self.assertEqual(next(run.glob('attempts/000002/*/failed_result.jsonl')).read_text(), '{broken')

    def test_cancellation_stops_worker_and_removes_its_runtime(self):
        _, run = self.prepare(['slow'])
        proc = subprocess.Popen(self.command(run), cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                traces = list(run.glob('attempts/*/*/trace.jsonl'))
                if traces and 'command_execution' in traces[0].read_text():
                    break
                time.sleep(0.05)
            else:
                self.fail('fake worker did not start')
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 130, stdout + stderr)
            self.assert_clean()
            status = screening.load_json(next(run.glob('attempts/*/*/status.json')))
            self.assertEqual(status['status'], 'cancelled')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_api_reader_uses_shared_records_and_cleans_up(self):
        requests = []
        case = self
        repository_text = 'The repository contains an evaluation script.'

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, body))
                self.send_response(200)
                self.end_headers()
                if self.path == '/v1/tools/fetch':
                    self.wfile.write(json.dumps({'content': repository_text}).encode())
                    return
                if body.get('tools'):
                    text = 'Author material: https://example.org/repo'
                elif stage == 'detail':
                    record = json.loads(json.dumps(case.records['good']))
                    record.pop('model')
                    if detail_sources == 'web':
                        record['evidence'].append({'source_id': 'web_001', 'locator': 'README',
                                                   'quote': repository_text, 'supports': ['implementation']})
                    if bad_quote:
                        record['evidence'][0]['quote'] = 'This excerpt is absent from the paper.'
                    text = json.dumps(record)
                else:
                    record = {k: v for k, v in case.records['good'].items() if k in screening.COMMON and k != 'model'}
                    record.update(stage='triage', reading_scope='metadata_only', decision='priority',
                                  evidence=[{'source_id': 'metadata', 'locator': 'Abstract',
                                             'quote': case.papers['good']['abstract'], 'supports': ['main_content']}])
                    text = json.dumps(record)
                event = {'type': 'response.completed', 'response': {'status': 'completed', 'model': 'fixture-api',
                         'usage': {'input_tokens': 100, 'output_tokens': 30},
                         'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}}
                self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.reader_settings['api'].update(base_url=f'http://127.0.0.1:{server.server_port}',
                                          model='fixture-api', key_env='SCREENING_TEST_KEY')
        screening.write_json(self.readers, self.reader_settings)
        env_file = self.base / 'api.env'
        env_file.write_text('SCREENING_TEST_KEY=fixture\n')
        source = self.base / 'papers.jsonl'
        screening.write_rows(source, [self.papers['good']])
        try:
            for stage, provider, detail_sources, bad_quote in (
                ('detail', 'deepseek', 'web', False),
                ('detail', 'deepseek', 'supplied', False),
                ('triage', 'deepseek', 'web', False),
                ('detail', 'openai', None, True),
            ):
                requests.clear()
                self.reader_settings['api']['provider'] = provider
                if detail_sources is None:
                    self.reader_settings['api'].pop('detail_sources', None)
                else:
                    self.reader_settings['api']['detail_sources'] = detail_sources
                screening.write_json(self.readers, self.reader_settings)
                run = self.base / f'{stage}-{provider}-{detail_sources}'
                screening.prepare(source, run, stage, reader='api', readers_path=self.readers, batch_path=FIXTURES / 'batch.json')
                result = subprocess.run([sys.executable, '-m', 'scripts.data_collect', 'fine-screen', 'run', '--run', str(run),
                                         '--env-file', str(env_file), '--runtime', str(self.base / 'runtime')],
                                        cwd=ROOT, capture_output=True, text=True)
                if stage == 'triage' or detail_sources != 'web':
                    self.assertEqual(len(requests), 1)
                if bad_quote:
                    self.assertEqual(result.returncode, 2, result.stderr + result.stdout)
                    _, report = screening.collect_records(run)
                    self.assertIn('quote absent', report['execution_failures'][0]['error'])
                    self.assert_clean()
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rows, _ = screening.complete_records(run)
                self.assertEqual(rows[0]['assessment']['model'], 'fixture-api')
                self.assertEqual(rows[0]['score'], 2 / 3 if stage == 'detail' else None)
                if stage == 'detail' and detail_sources == 'web':
                    self.assertIn(repository_text, [entry['quote'] for entry in rows[0]['assessment']['evidence']])
                self.assertTrue(all(body['model'] == 'fixture-api' for path, body in requests if path == '/responses'))
                self.assertTrue(next(run.glob('attempts/*/*/trace.jsonl')).is_file())
                self.assert_clean()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
