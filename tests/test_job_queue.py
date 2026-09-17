"""Queue ordering, durable audio ownership, recovery, and concurrent capture."""
import fcntl
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox
from core import atomic_json, ensure_dirs, new_note, read_json, discard_pending
from job_queue import JobQueue, source_of
import gui
import worker


class FakeProcess:
    pid = 2**30  # Outside the OS process-id range; never signal a real process.
    def __init__(self, *args, **kwargs):
        self.code = None
    def poll(self):
        return self.code
    def wait(self, timeout=None):
        self.code = -15 if self.code is None else self.code
        return self.code


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'SORITAKER_DATA_DIR':self.temp.name})
        self.env.start()
        self.root = ensure_dirs()
        self.queue = JobQueue(self.root, recover=False)

    def tearDown(self):
        self.queue.shutdown()
        self.env.stop()
        self.temp.cleanup()

    def audio(self, name):
        path = self.root/'recordings'/(name+'.wav')
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(16000)
            out.writeframes(b'\x10\x00'*16000)
        return path

    def spec(self, name, kind='save_audio'):
        source = self.audio(name)
        spec = dict(kind=kind, export_name=name, export_folder=str(self.root/'exports'))
        if kind == 'transcribe':
            spec['note'] = new_note(str(source),'turbo','en',False,1,'')
        else:
            spec['source'] = str(source)
        return spec

    def complete(self, item):
        result = dict(state='complete',message='Saved',saved_paths=[],progress=100)
        atomic_json(self.queue.folder(item)/'result.json',result)
        self.queue.proc.code = 0
        self.queue.tick()


class QueueTests(Fixture):
    def test_fifo_and_frozen_settings_while_another_job_is_running(self):
        spec = self.spec('First','transcribe')
        with patch('job_queue.subprocess.Popen',side_effect=FakeProcess) as spawn:
            first = self.queue.enqueue(spec)
            self.queue.start_next()
            second = self.queue.enqueue(self.spec('Second'))
            spec['note']['language']='ko'
            spec['note']['model']='large'
            spec['export_folder']='/changed'
            self.queue.tick()
            self.assertEqual(spawn.call_count,1)
            frozen = read_json(self.queue.folder(first)/'job.json')
            self.assertEqual(frozen['note']['language'],'en')
            self.assertEqual(frozen['note']['model'],'turbo')
            self.assertEqual(frozen['export_folder'],str(self.root/'exports'))
            self.complete(first)
            self.assertIs(self.queue.active,second)
            self.assertEqual(spawn.call_count,2)

    def test_handoff_failure_restores_original_audio(self):
        spec = self.spec('Keep')
        original = Path(spec['source']).read_bytes()
        with patch.object(self.queue,'persist',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.queue.enqueue(spec)
        self.assertEqual(Path(spec['source']).read_bytes(),original)
        self.assertEqual(list((self.root/'jobs').iterdir()),[])
        self.assertEqual(self.queue.items,[])

    def test_pending_cancellation_preserves_its_audio_and_next_task(self):
        with patch('job_queue.subprocess.Popen',side_effect=FakeProcess):
            first = self.queue.enqueue(self.spec('First'))
            cancelled = self.queue.enqueue(self.spec('Cancelled'))
            last = self.queue.enqueue(self.spec('Last'))
            self.queue.start_next()
            self.queue.cancel(cancelled)
            self.complete(first)
            self.assertIs(self.queue.active,last)
            self.assertTrue(Path(source_of(cancelled['spec'])).exists())
            self.assertTrue(self.queue.folder(cancelled).exists())
            self.assertEqual(cancelled['state'],'cancelled')

    def test_failed_worker_keeps_audio_and_advances(self):
        with patch('job_queue.subprocess.Popen',side_effect=FakeProcess):
            first = self.queue.enqueue(self.spec('Fail'))
            last = self.queue.enqueue(self.spec('Last'))
            self.queue.start_next()
            atomic_json(self.queue.folder(first)/'status.json',dict(state='error',message='Cannot decode'))
            self.queue.proc.code = 1
            self.queue.tick()
            self.assertEqual(first['state'],'failed')
            self.assertTrue(Path(source_of(first['spec'])).exists())
            self.assertIs(self.queue.active,last)

    def test_process_start_failure_preserves_audio(self):
        item = self.queue.enqueue(self.spec('Keep'))
        with patch('job_queue.subprocess.Popen',side_effect=OSError('cannot start')):
            self.queue.start_next()
        self.assertEqual(item['state'],'failed')
        self.assertTrue(Path(source_of(item['spec'])).exists())
        self.assertIsNone(self.queue.log)

    def test_retry_goes_after_tasks_already_waiting(self):
        first = self.queue.enqueue(self.spec('Retry'))
        waiting = self.queue.enqueue(self.spec('Waiting'))
        self.queue.cancel(first)
        self.queue.retry(first)
        self.assertEqual([item['id'] for item in self.queue.items],[waiting['id'],first['id']])
        restored = JobQueue(self.root)
        self.assertEqual([item['id'] for item in restored.items],[waiting['id'],first['id']])

    def test_shutdown_retains_pending_work_for_next_launch(self):
        with patch('job_queue.subprocess.Popen',side_effect=FakeProcess):
            first = self.queue.enqueue(self.spec('Interrupted'))
            waiting = self.queue.enqueue(self.spec('Waiting'))
            self.queue.start_next()
            self.queue.shutdown()
            self.assertEqual(first['state'],'interrupted')
            self.assertEqual(waiting['state'],'queued')
            self.assertTrue(all(Path(source_of(item['spec'])).is_file() for item in (first,waiting)))
        restored = JobQueue(self.root)
        self.assertEqual([item['state'] for item in restored.items],['interrupted','queued'])

    def test_success_receipt_wins_a_simultaneous_cancel(self):
        with patch('job_queue.subprocess.Popen',side_effect=FakeProcess):
            item = self.queue.enqueue(self.spec('Done'))
            self.queue.start_next()
            self.queue.cancel(item)
            self.assertEqual(item['state'],'cancelling')
            self.complete(item)
            self.assertEqual(item['state'],'complete')
            self.assertTrue((self.queue.folder(item)/'result.json').exists())

    def test_restart_restores_waiting_and_interrupted_work(self):
        first = self.queue.enqueue(self.spec('Interrupted'))
        waiting = self.queue.enqueue(self.spec('Waiting'))
        first['state']='running'
        self.queue.persist(first)
        restored = JobQueue(self.root)
        self.assertEqual([i['state'] for i in restored.items],['interrupted','queued'])
        self.assertTrue(all(Path(source_of(i['spec'])).exists() for i in restored.items))
        with patch('job_queue.subprocess.Popen',side_effect=FakeProcess):
            restored.start_next()
            self.assertEqual(restored.active['id'],waiting['id'])
            restored.shutdown()

    def test_recovery_during_enqueue_keeps_a_visible_task(self):
        item = self.queue.enqueue(self.spec('Recovered'))
        (self.queue.folder(item)/'queue.json').unlink()
        restored = JobQueue(self.root)
        self.assertEqual(restored.items[0]['state'],'interrupted')
        self.assertTrue(Path(source_of(restored.items[0]['spec'])).exists())

    def test_repeated_queue_worker_uses_receipt_without_duplicate_outputs(self):
        item = self.queue.enqueue(self.spec('One'))
        path = str(self.queue.folder(item)/'job.json')
        self.assertEqual(worker.run_job(path),0)
        self.assertEqual(worker.run_job(path),0)
        self.assertEqual([p.name for p in (self.root/'exports').iterdir()],['One.wav'])
        restored = JobQueue(self.root)
        self.assertEqual(restored.items[0]['state'],'complete')
        self.assertFalse(Path(source_of(item['spec'])).exists())

    def test_imported_original_and_queue_history_survive_other_cleanup(self):
        source = self.audio('Imported')
        external = self.root/'external.wav'
        source.replace(external)
        spec = dict(kind='save_audio',source=str(external),export_name='Output',export_folder=str(self.root/'exports'))
        item = self.queue.enqueue(spec)
        with self.assertRaises(ValueError):
            self.queue.enqueue(spec)
        self.queue.cancel(item)
        discard_pending(str(external))
        self.assertTrue(external.exists())
        self.assertTrue((self.queue.folder(item)/'queue.json').exists())


# This is a real worker process with real FFmpeg conversion and file exports.
# Only recognition is replaced, so the tests do not require MLX or model weights.
WORKER_ENTRY = '''
import os, sys, time, signal
from pathlib import Path
import worker
root=Path(os.environ['SORITAKER_DATA_DIR'])
worker.ready=lambda key: True
def recognize(audio, note, reporter):
    with (root/'recognition-calls').open('a') as f: f.write(note['language']+'\\n')
    (root/'recognition-started').touch()
    deadline=time.monotonic()+15
    while not (root/'release-worker').exists():
        if time.monotonic()>deadline: raise RuntimeError('Test gate timed out')
        time.sleep(0.01)
    if note['prompt']=='FAIL': raise RuntimeError('Recognition failed for test')
    return dict(language=note['language'],segments=[dict(start=0,end=0.8,text='A complete sentence.',words=[])])
worker.transcribe=recognize
signal.signal(signal.SIGTERM,worker.cancel_on_signal)
raise SystemExit(worker.run_job(sys.argv[-1]))
'''


class QueueIntegrationTests(Fixture):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_for(self, predicate, poll=lambda: None):
        deadline=time.monotonic()+12
        while not predicate() and time.monotonic()<deadline:
            poll()
            QApplication.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate(),'Timed out waiting for test worker / capture')

    def make_window(self):
        window=gui.Window(demo=True)
        window.timer.stop()
        window.queue.entry=[sys.executable,'-c',WORKER_ENTRY]
        window.folder.setText(str(self.root/'exports'))
        self.queue=window.queue
        return window

    def test_real_worker_finishes_while_next_recording_is_paused_then_saves_both(self):
        window=self.make_window()
        source=self.audio('First')
        original=source.read_bytes()
        window.set_source(str(source))
        window.diar=False
        window.language='en'
        with patch.object(gui,'ready',return_value=True):
            window.save()
        first=self.queue.items[0]
        self.assertEqual(window.source,'')
        self.assertTrue(window.record_button.isEnabled())
        self.wait_for(lambda:(self.root/'recognition-started').exists(),window.poll)
        block=b'\x20\x00'*16000
        class Input:
            def __init__(self,**kwargs): self.callback=kwargs['callback']
            def __enter__(self):
                self.callback(block,16000,None,None)
                return self
            def __exit__(self,*args): pass
        fake_device=types.SimpleNamespace(query_devices=lambda *args:dict(default_samplerate=16000),RawInputStream=Input)
        try:
            with patch.dict(sys.modules,{'sounddevice':fake_device}), patch.object(gui.RecordSetup,'exec',return_value=QDialog.Accepted):
                window.record()
                rec=window.recording
                self.wait_for(lambda:rec.samples_written>=16000)
                window.pause()
                self.assertEqual(window.state.text(),'PAUSED')
                window.language='ko'
                window.model='large'
                (self.root/'release-worker').touch()
                self.wait_for(lambda:first['state']=='complete',window.poll)
                self.assertNotIn(first['id'],window.queue_rows)
                self.assertTrue(window.queue_panel.isHidden())
                self.assertIs(window.recording,rec)
                self.assertEqual(window.state.text(),'PAUSED')
                self.assertTrue(window.stop_button.isEnabled())
                self.assertTrue(rec.paused.is_set())
                self.assertEqual(first['spec']['note']['language'],'en')
                self.assertEqual((self.root/'exports'/'First.wav').read_bytes(),original)
                window.pause()
                rec.accept_audio(block)
                window.stop()
                rec.join(timeout=3)
                self.assertFalse(rec.is_alive())
                window.finished_recording()
                self.assertTrue(window.save_dialog.isVisible())
            window.filename.setText('Second')
            window.save_mode.setCurrentIndex(1)
            window.save()
            second=self.queue.items[1]
            self.wait_for(lambda:second['state']=='complete',window.poll)
            with wave.open(str(self.root/'exports'/'Second.wav'),'rb') as audio:
                self.assertEqual(audio.getnframes(),32000)
            self.assertTrue((self.root/'exports'/'First.txt').is_file())
            self.assertFalse((self.root/'exports'/'Second.txt').exists())
            self.assertEqual(window.queue_view.topLevelItemCount(),0)
            self.assertTrue(window.queue_panel.isHidden())
            self.assertFalse(window.save_dialog.isVisible())
            restored=JobQueue(self.root)
            self.assertEqual([item['state'] for item in restored.items],['complete','complete'])
            window.queue=restored
            window.refresh_queue()
            self.assertEqual(window.queue_view.topLevelItemCount(),0)
            self.assertTrue(window.record_button.isEnabled())
        finally:
            if window.recording:
                window.recording.stop_event.set()
                window.recording.join(timeout=3)
                window.recording=None
            self.queue.shutdown()
            window.close()
            window.deleteLater()

    def test_os_lock_serializes_workers_and_receipt_prevents_a_second_export(self):
        item=self.queue.enqueue(self.spec('Locked','transcribe'))
        command=[sys.executable,'-c',WORKER_ENTRY,'--worker',str(self.queue.folder(item)/'job.json')]
        with (self.root/'processing.lock').open('a') as lock, (self.root/'test-worker.log').open('w') as log:
            fcntl.flock(lock,fcntl.LOCK_EX)
            first=subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True)
            second=None
            try:
                self.wait_for(lambda:read_json(self.queue.folder(item)/'status.json',{}).get('message')=='Waiting for the previous worker…')
                self.assertFalse((self.root/'recognition-started').exists())
                fcntl.flock(lock,fcntl.LOCK_UN)
                self.wait_for(lambda:(self.root/'recognition-started').exists())
                second=subprocess.Popen(command,stdout=log,stderr=log,start_new_session=True)
                (self.root/'release-worker').touch()
                self.assertEqual(first.wait(timeout=10),0)
                self.assertEqual(second.wait(timeout=10),0)
                self.assertEqual((self.root/'recognition-calls').read_text(),'en\n')
                self.assertEqual(sorted(p.name for p in (self.root/'exports').iterdir()),['Locked.txt','Locked.wav'])
            finally:
                for process in (first,second):
                    if process and process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)

    def test_failed_and_cancelled_background_jobs_leave_foreground_capture_untouched(self):
        window=self.make_window()
        rec=gui.Recorder(self.audio('Current'))
        rec.is_alive=lambda:True
        try:
            with patch('job_queue.subprocess.Popen',side_effect=FakeProcess):
                first=window.start_job(self.spec('Bad'))
                second=window.start_job(self.spec('Cancel'))
                window.recording=rec
                window.state.setText('RECORDING')
                window.clock.setText('00:00:07')
                rec.accept_audio(b'\x01\x00'*128)
                atomic_json(self.queue.folder(first)/'status.json',dict(state='error',message='Test failure'))
                self.queue.proc.code=1
                with patch.object(QMessageBox,'warning') as warning:
                    window.poll()
                    warning.assert_not_called()
                self.assertEqual(first['state'],'failed')
                self.assertIs(self.queue.active,second)
                window.queue_view.setCurrentItem(window.queue_rows[second['id']])
                window.cancel_job()
                self.queue.proc.code=-15
                window.poll()
                self.assertEqual(second['state'],'cancelled')
                self.assertIs(window.recording,rec)
                self.assertEqual(window.state.text(),'RECORDING')
                self.assertFalse(rec.stop_event.is_set())
                rec.accept_audio(b'\x01\x00'*128)
                self.assertEqual(rec.frames.qsize(),2)
        finally:
            window.recording=None
            self.queue.shutdown()
            window.close()
            window.deleteLater()


if __name__=='__main__':
    unittest.main()
