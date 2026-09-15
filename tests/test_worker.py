import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import numpy as np
import soundfile as sf
import worker
from core import new_note, read_json, atomic_json, note_dir, save_note


class JobTests(unittest.TestCase):
    def test_real_audio_conversion_and_saved_transcript_checkpoint(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'SORITAKER_DATA_DIR': d}):
            source = Path(d) / '원본.wav'
            t = np.arange(32000) / 16000
            sf.write(source, 0.1 * np.sin(2 * np.pi * 440 * t), 16000)
            note = new_note(str(source), 'turbo', 'ko', True, 0, '')
            save_note(note)
            job = Path(d) / 'job.json'
            atomic_json(job, dict(kind='transcribe', note=note))
            mock_result = dict(language='ko', segments=[dict(start=0, end=1.5, text='시험 결과', words=[])])
            with patch.object(worker, 'ready', return_value=True), patch.object(worker, 'transcribe', return_value=mock_result), \
                 patch.object(worker, 'diarize', side_effect=RuntimeError('speaker failed')):
                self.assertEqual(worker.run_job(str(job)), 0)
            stored = read_json(note_dir(note) / 'note.json')
            self.assertEqual(stored['segments'][0]['text'], '시험 결과')
            self.assertIn('speaker failed', stored['warnings'][0])
            self.assertTrue(Path(stored['original']).exists())
            self.assertTrue(Path(stored['audio']).exists())
            self.assertTrue(source.exists())
            self.assertEqual(read_json(Path(d)/'status.json')['state'], 'complete')

    def test_missing_model_is_error_without_network_download(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {'SORITAKER_DATA_DIR': d}):
            note = new_note('/missing.wav', 'turbo', 'ko', False, 0, '')
            job = Path(d) / 'job.json'
            atomic_json(job, dict(kind='transcribe', note=note))
            def check_local(key):
                self.assertNotIn('HF_HUB_OFFLINE',os.environ)
                self.assertNotIn('TRANSFORMERS_OFFLINE',os.environ)
                return False
            with patch.dict(os.environ,{'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}), \
                 patch.object(worker, 'ready', side_effect=check_local), patch.object(worker, 'install') as installer:
                self.assertEqual(worker.run_job(str(job)), 1)
                installer.assert_not_called()
            self.assertEqual(read_json(Path(d)/'status.json')['state'], 'error')


if __name__ == '__main__':
    unittest.main()
