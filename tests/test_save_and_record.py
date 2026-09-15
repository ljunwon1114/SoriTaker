import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core import save_outputs
from gui import Recorder


class SaveAndRecordTests(unittest.TestCase):
    def test_pause_excludes_audio_and_resume_restarts_capture(self):
        rec=Recorder(Path('/tmp/not-written.wav'))
        chunk=b'\x01\x00'*128
        rec.accept_audio(chunk)
        self.assertEqual(rec.frames.qsize(),1)
        rec.paused.set()
        rec.accept_audio(chunk)
        self.assertEqual(rec.frames.qsize(),1)
        rec.paused.clear()
        rec.accept_audio(chunk)
        self.assertEqual(rec.frames.qsize(),2)
        rec.stop_event.set()
        rec.accept_audio(chunk)
        self.assertEqual(rec.frames.qsize(),2)

    def test_save_pair_does_not_overwrite_existing_files(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            original=root/'source.wav'
            original.write_bytes(b'original audio')
            out=root/'out';out.mkdir()
            (out/'Meeting.txt').write_text('keep me')
            note=dict(title='Meeting',original=str(original),segments=[dict(start=0,end=1,text='한국어 English',speaker='Speaker 1')])
            saved=save_outputs(note,out,'Meeting')
            self.assertEqual([Path(p).name for p in saved],['Meeting (2).wav','Meeting (2).txt'])
            self.assertEqual((out/'Meeting.txt').read_text(),'keep me')
            self.assertFalse((out/'Meeting.wav').exists())
            self.assertEqual(Path(saved[0]).read_bytes(),original.read_bytes())
            self.assertIn('한국어 English',Path(saved[1]).read_text())

    def test_failed_audio_copy_leaves_existing_files_intact(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);original=root/'source.wav';original.write_bytes(b'audio')
            out=root/'out';out.mkdir();(out/'keep.txt').write_text('safe')
            note=dict(original=str(original),segments=[])
            with patch('shutil.copyfileobj',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):save_outputs(note,out,'Meeting')
            self.assertEqual([p.name for p in out.iterdir()],['keep.txt'])
            self.assertTrue(original.exists())


if __name__=='__main__':unittest.main()
