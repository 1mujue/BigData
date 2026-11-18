"""Read one paper through an isolated Codex Exec process."""
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time

from . import screening as s

WORKING_INSTRUCTIONS = (
    "The assigned paper is paper.json; source paths are listed in it. "
    "Follow the reading scope in the task prompt. Save any allowed supplementary source text locally "
    "and register its relative file path in extra_sources. "
    "Write exactly one record to results.jsonl. The program supplies the model field. "
    "Before finishing, run check_command from assignment.json and correct reported errors. "
    "Save downloads and helper files in this working directory or the supplied TMPDIR."
)


def environment(home, codex_home, tmp):
    keep = {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'HTTP_PROXY', 'HTTPS_PROXY',
            'ALL_PROXY', 'NO_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy',
            'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(HOME=str(home), CODEX_HOME=str(codex_home), TMPDIR=str(tmp),
               SHELL='/bin/bash', TERM='dumb', PYTHONDONTWRITEBYTECODE='1')
    return env


def overrides(reader, stage):
    config = {
        'model_reasoning_effort': reader['effort'],
        'agents.enabled': False, 'project_doc_max_bytes': 0, 'mcp_servers': {},
        'features.apps': False, 'features.plugins': False, 'features.hooks': False,
        'features.memories': False, 'skills.include_instructions': False,
        'skills.bundled.enabled': False, 'allow_login_shell': False,
        'approval_policy': 'never', 'sandbox_mode': 'workspace-write',
        'sandbox_workspace_write.network_access': stage == 'detail',
        'web_search': 'live' if stage == 'detail' else 'disabled',
    }
    args = []
    for key, value in config.items():
        args += ['-c', key + '=' + json.dumps(value)]
    return args


def signal_process_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        # macOS may report EPERM after the owned process has exited.
        if proc.poll() is None:
            raise


def stop_process_group(proc):
    # The exec process owns this group; --no-daemon avoids a shared persistent server.
    signal_process_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    signal_process_group(proc, signal.SIGKILL)
    proc.wait()


def run_process(argv, env, cwd, stdout, stderr, cancel, timeout, stdin=None):
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=stdin or subprocess.DEVNULL,
                            stdout=stdout, stderr=stderr, start_new_session=True)
    deadline = time.monotonic() + timeout
    try:
        while proc.poll() is None:
            if cancel.is_set():
                return 'cancelled', None
            if time.monotonic() >= deadline:
                return 'timed_out', None
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        return ('completed' if proc.returncode == 0 else 'failed'), proc.returncode
    finally:
        stop_process_group(proc)


def layout(base):
    work, home, codex_home, tmp = [base / name for name in ('work', 'home', 'codex-home', 'tmp')]
    for path in (work, home, codex_home, tmp):
        path.mkdir()
    return work, home, codex_home, tmp


def populate(work, run, paper, task, stage):
    if stage == 'triage':
        paper = paper | {'sources': []}
    s.write_json(work / 'input.json', paper)
    metadata = {k: v for k, v in paper.items() if k != 'sources'}
    metadata['sources'] = []
    (work / 'sources').mkdir()
    for index, source in enumerate(s.source_map(paper, []).values(), 1):
        relative = f'sources/{index:03d}.txt'
        (work / relative).write_text(source['text'], encoding='utf-8')
        metadata['sources'].append({k: v for k, v in source.items() if k != 'text'} | {'path': relative})
    s.write_json(work / 'paper.json', metadata)
    (work / 'prompt.md').write_text((run / 'prompt.md').read_text() + '\n\n' + WORKING_INSTRUCTIONS, encoding='utf-8')
    shutil.copyfile(run / 'reader.json', work / 'reader.json')
    for name in ('rubric.json', 'batch.json'):
        shutil.copyfile(run / 'contract' / name, work / name)
    shutil.copyfile(s.ROOT / 'screening.py', work / 'check_result.py')
    s.write_json(work / 'assignment.json', {
        'paper_id': task['paper_id'], 'stage': stage,
        'check_command': shlex.join([sys.executable, '-c', 'from check_result import check_work; print(check_work("."))']),
    })


def trace_summary(path):
    completed, usage, errors, parse_errors = False, None, [], []
    # Preserve the native event stream, including any repeated item keys.
    with Path(path).open('rb') as source:
        for number, line in enumerate(source, 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('event is not an object')
            except (ValueError, UnicodeError) as error:
                parse_errors.append({'line': number, 'error': str(error)})
                continue
            if event.get('type') == 'turn.completed':
                completed, usage = True, event.get('usage')
            elif event.get('type') == 'turn.failed':
                completed = False
                errors.append(event.get('error'))
    return {'turn_completed': completed, 'usage': usage, 'turn_errors': errors,
            'trace_parse_errors': parse_errors}


def setup(_reader, args):
    s.require(args.auth_file and args.auth_file.is_file(), 'provide an existing authentication file with --auth-file')
    executable = shutil.which(args.codex)
    s.require(executable, 'codex executable was not found')
    version = subprocess.check_output([executable, '--version'], text=True).strip()
    return {'executable': executable, 'auth_file': args.auth_file.resolve(), 'version': version}


def read(work, run, paper, task, reader, stage, attempt, cancel, timeout, connection, status):
    work, home, codex_home, tmp = layout(work.parent)
    (codex_home / 'auth.json').symlink_to(connection['auth_file'])
    populate(work, run, paper, task, stage)
    argv = [connection['executable'], '--no-daemon', 'exec', '--json', '--ephemeral', '--ignore-user-config',
            '--ignore-rules', '--skip-git-repo-check', '--color', 'never',
            '-m', reader['model'], '-C', str(work), *overrides(reader, stage), '-']
    status['codex_version'] = connection['version']
    with (work / 'prompt.md').open('rb') as prompt, (attempt / 'trace.jsonl').open('wb') as trace, \
            (attempt / 'stderr.log').open('wb') as stderr:
        status['status'], status['exit_code'] = run_process(argv, environment(home, codex_home, tmp),
                                                          work, trace, stderr, cancel, timeout, prompt)
    status.update(trace_summary(attempt / 'trace.jsonl'))
    if status['status'] == 'completed':
        s.require(status['turn_completed'], 'exec exited without a completed turn')
