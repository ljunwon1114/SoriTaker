"""Persistent serial workers, independent of the microphone and foreground form."""
from __future__ import annotations

import copy
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from core import atomic_json, read_json, note_dir
from models import clear_legacy_offline_flags

ACTIVE = {'queued', 'running', 'cancelling'}
RETRYABLE = {'failed', 'cancelled', 'interrupted'}
CLEARABLE = RETRYABLE | {'complete'}


def source_of(spec):
    return spec.get('note', {}).get('source', '') if spec['kind'] == 'transcribe' else spec.get('source', '')


class JobQueue:
    def __init__(self, root, recover=True, entry=None):
        self.root = Path(root)
        self.items = []
        self.proc = None
        self.log = None
        self.active = None
        self.cancel_deadline = None
        self.stopping = False
        self.entry = entry
        if recover:
            for folder in (self.root/'jobs').iterdir():
                if folder.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', folder.name):
                    continue
                item = read_json(folder/'queue.json')
                spec = read_json(folder/'job.json')
                if not isinstance(spec, dict) or not spec.get('queue_managed'):
                    continue
                if not isinstance(item, dict):
                    item = dict(created=folder.stat().st_mtime_ns, state='interrupted',
                                message='Recovered unfinished task. Retry or save the audio file.')
                    source = source_of(spec)
                    original = Path(spec.get('input_original', ''))
                    if source and not Path(source).is_file() and original.is_file() and original.resolve().parent == (self.root/'recordings').resolve():
                        if spec['kind'] == 'transcribe':
                            spec['note']['source'] = str(original)
                        else:
                            spec['source'] = str(original)
                        atomic_json(folder/'job.json', spec)
                item.update(id=folder.name, spec=spec)
                result = read_json(folder/'result.json', {})
                if result.get('state') == 'complete':
                    item.update(state='complete', result=result)
                elif item.get('state') in ('running', 'cancelling'):
                    item.update(state='interrupted', message='Interrupted. Audio is preserved; retry when ready.')
                self.items.append(item)
                self.persist(item)
                if item['state'] == 'complete':
                    self.cleanup(item)
            self.items.sort(key=lambda item: item['created'])

    def folder(self, item):
        return self.root/'jobs'/item['id']

    def persist(self, item):
        atomic_json(self.folder(item)/'queue.json', {k:v for k,v in item.items() if k != 'spec'})

    def owns_source(self, source):
        path = Path(source).resolve()
        return any(item['state'] in ACTIVE and source_of(item['spec']) and
                   Path(source_of(item['spec'])).resolve() == path for item in self.items)

    def enqueue(self, spec):
        spec = copy.deepcopy(spec)
        source = source_of(spec)
        if source and self.owns_source(source):
            raise ValueError('This file is already in the queue.')
        if spec['kind'] == 'download':
            existing = next((item for item in self.items if item['state'] in ACTIVE and
                item['spec']['kind'] == 'download' and item['spec']['model'] == spec['model']), None)
            if existing:
                return existing
        item = dict(id=uuid.uuid4().hex, created=time.time_ns(), state='queued',
                    message='Waiting', spec=spec)
        folder = self.folder(item)
        folder.mkdir()
        moved = None
        try:
            if source:
                original = Path(source)
                if not original.is_file():
                    raise FileNotFoundError('The original audio file is missing.')
                # Claim only app-owned microphone recordings. Imported files stay in place.
                if not original.is_symlink() and original.resolve().parent == (self.root/'recordings').resolve():
                    moved = (original, folder/'recording.wav')
                    spec['input_original'] = str(original)
                    if spec['kind'] == 'transcribe':
                        spec['note']['source'] = str(moved[1])
                    else:
                        spec['source'] = str(moved[1])
            spec['queue_managed'] = True
            atomic_json(folder/'job.json', spec)
            if moved:
                moved[0].replace(moved[1])
            self.persist(item)
        except Exception:
            if moved and moved[1].exists():
                moved[1].replace(moved[0])
            # No worker has started; remove this incomplete queue entry only.
            for name in ('job.json', 'queue.json'):
                (folder/name).unlink(missing_ok=True)
            folder.rmdir()
            raise
        self.items.append(item)
        return item

    def start_next(self):
        if self.proc is not None or self.stopping:
            return
        item = next((item for item in self.items if item['state'] == 'queued'), None)
        if item is None:
            return
        folder = self.folder(item)
        item.update(state='running', message='Starting…')
        self.persist(item)
        entry = self.entry or ([sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).with_name('main.py'))])
        env = clear_legacy_offline_flags(os.environ.copy())
        env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        try:
            self.log = (folder/'worker.log').open('a', encoding='utf-8')
            self.proc = subprocess.Popen(entry+['--worker', str(folder/'job.json')],
                stdout=self.log, stderr=self.log, env=env, start_new_session=True)
        except Exception as exc:
            if self.log:
                self.log.close()
            self.log = None
            item.update(state='failed', message=str(exc))
            self.persist(item)
            return
        self.active = item
        if sys.platform == 'darwin':
            try:
                subprocess.Popen(['/usr/bin/caffeinate', '-i', '-w', str(self.proc.pid)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def cleanup(self, item):
        # Each queued microphone recording and working note belongs to one job.
        # Never remove another job, an imported original, or a saved output.
        import shutil
        source = source_of(item['spec'])
        folder = self.folder(item)
        try:
            if source and Path(source) == folder/'recording.wav':
                Path(source).unlink(missing_ok=True)
            if item['spec']['kind'] == 'transcribe':
                working = note_dir(item['spec']['note'])
                if working.is_dir() and not working.is_symlink():
                    shutil.rmtree(working)
        except OSError:
            pass

    def tick(self):
        # An orphaned worker from a previous GUI session may finish later.
        for item in self.items:
            if item['state'] == 'interrupted':
                result = read_json(self.folder(item)/'result.json', {})
                if result.get('state') == 'complete':
                    item.update(state='complete', result=result, message=result['message'])
                    self.persist(item)
                    self.cleanup(item)
        if self.proc is not None:
            item = self.active
            status = read_json(self.folder(item)/'status.json', {})
            if item['state'] != 'cancelling':
                item['message'] = status.get('message', item['message'])
            item['progress'] = status.get('progress')
            if self.cancel_deadline is not None and time.monotonic() >= self.cancel_deadline and self.proc.poll() is None:
                self.signal(signal.SIGKILL)
                self.cancel_deadline = None
            if self.proc.poll() is not None:
                result = read_json(self.folder(item)/'result.json', {})
                status = read_json(self.folder(item)/'status.json', status)
                if result.get('state') == 'complete':
                    item.update(state='complete', result=result, message=result['message'], progress=100)
                elif item['state'] == 'cancelling':
                    item.update(state='interrupted' if self.stopping else 'cancelled',
                                message='Audio is preserved. Retry or save the audio file.', progress=None)
                else:
                    item.update(state='failed', message=status.get('message') or 'Worker stopped. Audio is preserved.', progress=None)
                self.persist(item)
                self.log.close()
                self.log = None
                self.proc = None
                self.active = None
                self.cancel_deadline = None
                if item['state'] == 'complete':
                    self.cleanup(item)
        self.start_next()

    def signal(self, sig):
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(self, item):
        if item['state'] == 'queued':
            item.update(state='cancelled', message='Cancelled. Audio is preserved.')
            self.persist(item)
        elif item is self.active and item['state'] == 'running':
            item.update(state='cancelling', message='Cancelling…')
            self.persist(item)
            if self.proc.poll() is None:
                self.signal(signal.SIGTERM)
                self.cancel_deadline = time.monotonic()+2

    def retry(self, item):
        if item['state'] not in RETRYABLE:
            return
        source = source_of(item['spec'])
        if source and self.owns_source(source):
            raise ValueError('This file is already being processed in another queue entry.')
        item.update(state='queued', message='Waiting', progress=None, created=time.time_ns(), dismissed=False)
        # Do not erase completion receipts. The worker checks them under the lock.
        self.persist(item)
        self.items.remove(item)
        self.items.append(item)

    def clear_finished(self):
        """Dismiss finished rows durably; retain audio, logs, and export receipts."""
        count = 0
        for item in self.items:
            if item['state'] in CLEARABLE and not item.get('dismissed') and item is not self.active:
                updated = dict(item, dismissed=True)
                self.persist(updated)
                item['dismissed'] = True
                count += 1
        return count

    def shutdown(self):
        self.stopping = True
        if self.proc is not None:
            self.cancel(self.active)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.signal(signal.SIGKILL)
                self.proc.wait(timeout=3)
            self.tick()
