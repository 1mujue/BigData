"""Run a prepared screening batch with its selected reader."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time

from . import reader_api, reader_codex
from . import screening as s

READERS = {'codex': reader_codex, 'api': reader_api}


def execute(adapter, run, runtime, task, paper, manifest, rubric, batch, reader, connection, cancel, timeout):
    parent = run / 'attempts' / task['task_id']
    parent.mkdir(parents=True, exist_ok=True)
    number = len(list(parent.iterdir())) + 1
    attempt = parent / f'{number:03d}'
    attempt.mkdir()
    status = {'paper_id': task['paper_id'], 'attempt': number, 'started_at': time.time(),
              'reader_mode': reader['mode'], 'requested_model': reader['model'],
              'reader_effort': reader['effort'], 'status': 'failed'}
    try:
        with tempfile.TemporaryDirectory(prefix=task['task_id'] + '-', dir=runtime) as folder:
            work = Path(folder) / 'work'
            accepted = False
            try:
                adapter.read(work, run, paper, task, reader, manifest['stage'], attempt,
                             cancel, timeout, connection, status)
                if status['status'] == 'completed':
                    raw = s.read_work_record(work, reader)
                    s.save_record(run, task, paper, raw, work, rubric, batch, manifest['stage'], attempt)
                    accepted = True
            finally:
                if not accepted and (work / 'results.jsonl').exists():
                    shutil.copyfile(work / 'results.jsonl', attempt / 'failed_result.jsonl')
    except InterruptedError:
        status['status'] = 'cancelled'
    except TimeoutError as error:
        status.update(status='timed_out', error=str(error))
    except (ValueError, OSError, KeyError, TypeError) as error:
        status.update(status='failed', error=str(error))
    finally:
        status['finished_at'] = time.time()
        s.write_json(attempt / 'status.json', status)
        log = attempt / 'stderr.log'
        if log.exists() and log.stat().st_size == 0:
            log.unlink()
    return status


def run_readers(args):
    run, runtime = args.run.resolve(), args.runtime.resolve()
    s.require(args.timeout > 0 and (args.limit is None or args.limit > 0), 'timeout and limit must be positive')
    manifest, rubric, batch = s.load_run(run)
    reader = s.load_json(run / 'reader.json')
    adapter = READERS[reader['mode']]
    _, checked = s.collect_records(run)
    s.require(not checked['errors'],
              'retained records are invalid; use python -m scripts.data_collect fine-screen check --run <run-directory>')
    if checked['complete']:
        return checked
    runtime.mkdir(parents=True, exist_ok=True)
    lock = run / '.running'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise ValueError('run is locked; check the owning process before removing .running')
    with os.fdopen(fd, 'w') as f:
        f.write(str(os.getpid()))
    cancel = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_: cancel.set()) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        connection = adapter.setup(reader, args)
        queued = (pair for pair in s.assigned_papers(manifest)
                  if not (run / 'records' / (pair[0]['task_id'] + '.json')).exists())
        statuses, submitted = [], 0
        with ThreadPoolExecutor(max_workers=batch['max_concurrency']) as executor:
            active = set()
            while True:
                while not cancel.is_set() and len(active) < batch['max_concurrency'] and (args.limit is None or submitted < args.limit):
                    pair = next(queued, None)
                    if pair is None:
                        break
                    task, paper = pair
                    active.add(executor.submit(execute, adapter, run, runtime, task, paper, manifest,
                                               rubric, batch, reader, connection, cancel, args.timeout))
                    submitted += 1
                if not active:
                    break
                finished, active = wait(active, return_when=FIRST_COMPLETED)
                statuses.extend(f.result() for f in finished)
        _, checked = s.collect_records(run)
        checked['attempted'] = len(statuses)
        checked['cancelled'] = cancel.is_set()
        checked['attempts_succeeded'] = all(x['status'] == 'completed' for x in statuses)
        return checked
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        lock.unlink()
