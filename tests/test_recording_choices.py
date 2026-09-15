import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, Mock

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton
from core import (ensure_dirs, new_note, note_dir, save_note, atomic_json,
                  read_json, save_audio_output, discard_pending)
import gui
import worker


class RecordingChoiceFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'SORITAKER_DATA_DIR': self.temporary.name})
        self.env.start()
        self.root = ensure_dirs()
        self.source = self.root / 'recordings' / 'current.wav'
        self.source.write_bytes(b'original audio' * 16)

    def tearDown(self):
        self.env.stop()
        self.temporary.cleanup()

    def partial_attempt(self, source):
        note = new_note(str(source), 'turbo', 'ko', False, 0, '')
        note['segments'] = [dict(start=0, end=1, text='temporary transcript')]
        save_note(note)
        (note_dir(note) / 'original.wav').write_bytes(b'working copy')
        job = self.root / 'jobs' / uuid.uuid4().hex
        atomic_json(job / 'job.json', dict(kind='transcribe', note=note))
        return note_dir(note), job


class RecordingChoiceTests(RecordingChoiceFixture, unittest.TestCase):
    def test_audio_only_is_exact_copy_and_preserves_collisions(self):
        out = self.root / 'exports'
        out.mkdir()
        (out / 'Meeting.wav').write_bytes(b'existing')
        (out / 'Meeting.txt').write_text('existing transcript')
        saved = save_audio_output(self.source, out, 'Meeting')
        self.assertEqual([Path(p).name for p in saved], ['Meeting (2).wav'])
        self.assertEqual(Path(saved[0]).read_bytes(), self.source.read_bytes())
        self.assertEqual((out / 'Meeting.wav').read_bytes(), b'existing')
        self.assertEqual((out / 'Meeting.txt').read_text(), 'existing transcript')
        self.assertFalse((out / 'Meeting (2).txt').exists())

    def test_audio_only_worker_needs_no_model_or_conversion(self):
        note_folder, old_job = self.partial_attempt(self.source)
        job = self.root / 'jobs' / uuid.uuid4().hex
        spec = job / 'job.json'
        atomic_json(spec, dict(kind='save_audio', source=str(self.source),
                    export_folder=str(self.root / 'exports'), export_name='Recording'))
        original = self.source.read_bytes()
        with patch.object(worker, 'ready', side_effect=AssertionError('model check')), \
             patch.object(worker, 'install', side_effect=AssertionError('download')), \
             patch.object(worker, 'convert_audio', side_effect=AssertionError('conversion')), \
             patch.object(worker, 'transcribe', side_effect=AssertionError('transcription')):
            self.assertEqual(worker.run_job(str(spec)), 0)
        status = read_json(job / 'status.json')
        self.assertEqual(status['state'], 'complete')
        self.assertEqual(len(status['saved_paths']), 1)
        self.assertEqual(Path(status['saved_paths'][0]).read_bytes(), original)
        self.assertFalse(self.source.exists())
        self.assertFalse(note_folder.exists())
        self.assertFalse(old_job.exists())
        self.assertTrue(job.exists())

    def test_failed_audio_only_save_preserves_source_and_removes_new_output(self):
        out = self.root / 'exports'
        with patch('shutil.copyfileobj', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                save_audio_output(self.source, out, 'Recording')
        self.assertTrue(self.source.exists())
        self.assertEqual(list(out.iterdir()), [])

    def test_discard_removes_only_current_unsaved_work(self):
        folder, job = self.partial_attempt(self.source)
        other = self.root / 'recordings' / 'other.wav'
        other.write_bytes(b'keep other recording')
        other_folder, other_job = self.partial_attempt(other)
        exported = self.root / 'Saved.txt'
        exported.write_text('already saved')
        # Even a manifest mentioning an exported result must not delete that result.
        atomic_json(job / 'status.json', dict(saved_paths=[str(exported)]))
        discard_pending(str(self.source))
        self.assertFalse(self.source.exists())
        self.assertFalse(folder.exists())
        self.assertFalse(job.exists())
        self.assertTrue(other.exists())
        self.assertTrue(other_folder.exists())
        self.assertTrue(other_job.exists())
        self.assertTrue(exported.exists())

    def test_discard_keeps_imported_original_and_skips_symlinked_work(self):
        external = self.root / 'imported.wav'
        external.write_bytes(b'keep original')
        folder, job = self.partial_attempt(external)
        outside = self.root / 'outside'
        outside.mkdir()
        atomic_json(outside / 'note.json', dict(source=str(external)))
        linked = self.root / 'notes' / uuid.uuid4().hex
        linked.symlink_to(outside, target_is_directory=True)
        discard_pending(str(external))
        self.assertTrue(external.exists())
        self.assertFalse(folder.exists())
        self.assertFalse(job.exists())
        self.assertTrue(linked.is_symlink())
        self.assertTrue((outside / 'note.json').exists())

    def test_audio_only_rejects_path_traversal(self):
        for name in ('../outside', r'..\outside', '.', '', 'bad\nname'):
            with self.assertRaises(ValueError):
                save_audio_output(self.source, self.root / 'exports', name)


class RecordingChoiceGUITests(RecordingChoiceFixture, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        super().setUp()
        self.window = gui.Window(demo=True)
        self.window.timer.stop()
        self.window.folder.setText(str(self.root / 'exports'))

    def tearDown(self):
        self.window.recording = None
        self.window.proc = None
        self.window.close()
        self.window.deleteLater()
        super().tearDown()

    def test_cancelling_setup_preserves_pending_recording_and_never_starts_microphone(self):
        self.window.set_source(str(self.source))
        with patch.object(gui.RecordSetup, 'exec', return_value=QDialog.Rejected), \
             patch.object(gui, 'Recorder') as recorder:
            self.window.record()
        recorder.assert_not_called()
        self.assertEqual(self.window.source, str(self.source))
        self.assertTrue(self.source.exists())

    def test_popup_selections_apply_before_recording_starts(self):
        def choose(dialog):
            dialog.language.setCurrentIndex(dialog.language.findData('en'))
            dialog.model.setCurrentIndex(dialog.model.findData('large'))
            return QDialog.Accepted
        with patch.object(gui.RecordSetup, 'exec', choose), patch.object(gui, 'Recorder') as recorder:
            self.window.record()
            recorder.return_value.start.assert_called_once()
        self.assertEqual(self.window.language, 'en')
        self.assertEqual(self.window.model, 'large')
        self.assertEqual(self.window.recording_mode, 'lecture')
        self.assertFalse(self.window.diar)
        self.assertEqual(self.window.speakers, 1)
        self.assertTrue(self.window.options_button.isEnabled())
        self.assertEqual(read_json(self.root / 'settings.json')['language'], 'en')
        self.assertTrue(self.window.save_panel.isHidden())

    def test_mid_recording_options_preserve_capture_and_save_uses_latest_settings(self):
        rec=gui.Recorder(self.source,device=3)
        self.window.device=3
        self.window.recording=rec
        chunk=b'\x01\x00'*128
        rec.accept_audio(chunk)
        def edit(dialog):
            self.assertFalse(dialog.device.isEnabled())
            self.assertTrue(dialog.language.isEnabled())
            self.assertTrue(dialog.model.isEnabled())
            downloads=[b for b in dialog.findChildren(QPushButton) if b.text()=='Download']
            self.assertEqual(len(downloads),3)
            self.assertTrue(all(not b.isEnabled() for b in downloads))
            dialog.language.setCurrentIndex(dialog.language.findData('en'))
            dialog.model.setCurrentIndex(dialog.model.findData('large'))
            dialog.diar.setChecked(True)
            dialog.speakers.setValue(2)
            dialog.prompt.setText('CRISPR, Mre11, Rad50')
            dialog.commit()
            return QDialog.Accepted
        with patch.object(gui,'ready',return_value=False), patch.object(gui.Options,'exec',edit):
            self.window.open_options()
        self.assertIs(self.window.recording,rec)
        self.assertEqual(self.window.device,3)
        self.assertFalse(rec.stop_event.is_set())
        self.assertFalse(rec.paused.is_set())
        rec.accept_audio(chunk)
        self.assertEqual(rec.frames.qsize(),2)
        self.window.stop()
        self.window.finished_recording()
        with patch.object(gui,'ready',return_value=True), patch.object(self.window,'start_job') as start_job:
            self.window.save()
        note=start_job.call_args.args[0]['note']
        self.assertEqual((note['language'],note['model'],note['speaker_count']),('en','large',2))
        self.assertTrue(note['diarize'])
        self.assertEqual(note['prompt'],'CRISPR, Mre11, Rad50')

    def test_options_remain_available_when_paused_but_reject_keeps_settings(self):
        rec=gui.Recorder(self.source)
        self.window.recording=rec
        self.window.pause()
        before=(self.window.language,self.window.model,self.window.prompt)
        def reject(dialog):
            dialog.language.setCurrentIndex(dialog.language.findData('en'))
            dialog.model.setCurrentIndex(dialog.model.findData('large'))
            dialog.reject()
            return QDialog.Rejected
        with patch.object(gui.Options,'exec',reject) as edit:
            self.window.open_options()
        self.assertEqual((self.window.language,self.window.model,self.window.prompt),before)
        self.assertTrue(rec.paused.is_set())
        self.window.pause()
        self.assertFalse(rec.paused.is_set())

    def test_open_recording_options_cannot_launch_download_or_worker(self):
        self.window.recording=gui.Recorder(self.source)
        dialog=gui.Options(self.window)
        with patch.object(self.window,'start_job') as start_job:
            dialog.download('large')
            start_job.assert_not_called()
        with patch.object(gui.subprocess,'Popen') as process:
            self.window.start_job(dict(kind='download',model='large'))
            process.assert_not_called()
        self.assertIsNone(self.window.proc)
        dialog.close()

    def test_gui_download_clears_legacy_flags_without_changing_parent(self):
        with patch.dict(os.environ,{'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}), \
             patch.object(gui.subprocess,'Popen') as process:
            self.window.start_job(dict(kind='download',model='large'))
            env=process.call_args.kwargs['env']
            self.assertNotIn('HF_HUB_OFFLINE',env)
            self.assertNotIn('TRANSFORMERS_OFFLINE',env)
            self.assertEqual(os.environ['HF_HUB_OFFLINE'],'1')
            self.assertEqual(os.environ['TRANSFORMERS_OFFLINE'],'1')
        self.window.job_log.close()
        self.window.job_log=None
        self.window.proc=None

    def test_stop_offers_choices_without_starting_any_worker(self):
        rec = Mock(path=self.source, duration=2, error='')
        self.window.recording = rec
        with patch.object(self.window, 'start_job') as start_job:
            self.window.stop()
            rec.stop_event.set.assert_called_once()
            self.window.finished_recording()
            start_job.assert_not_called()
        self.assertFalse(self.window.save_panel.isHidden())
        self.assertFalse(self.window.discard_button.isHidden())
        self.assertTrue(self.window.save_button.isEnabled())

    def test_audio_only_gui_skips_model_checks_and_note_creation(self):
        self.window.set_source(str(self.source))
        self.window.save_mode.setCurrentIndex(1)
        with patch.object(gui, 'ready', side_effect=AssertionError('model check')), \
             patch.object(gui, 'save_note', side_effect=AssertionError('note creation')), \
             patch.object(self.window, 'start_job') as start_job:
            self.window.save()
        spec = start_job.call_args.args[0]
        self.assertEqual(spec['kind'], 'save_audio')
        self.assertEqual(spec['source'], str(self.source))

    def test_discard_cancel_preserves_then_confirm_deletes_pending_work(self):
        folder, job = self.partial_attempt(self.source)
        self.window.set_source(str(self.source))
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.No):
            self.window.discard()
        self.assertTrue(self.source.exists())
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.window.discard()
        self.assertFalse(self.source.exists())
        self.assertFalse(folder.exists())
        self.assertFalse(job.exists())
        self.assertEqual(self.window.source, '')
        self.assertFalse(self.window.save_button.isEnabled())
        self.assertTrue(self.window.save_panel.isHidden())


if __name__ == '__main__':
    unittest.main()
