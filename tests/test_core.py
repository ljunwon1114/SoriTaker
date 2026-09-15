import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from core import *


class TranscriptTests(unittest.TestCase):
    def test_rename_reuses_existing_data_without_moving_files(self):
        with tempfile.TemporaryDirectory() as d, patch('core.Path.home', return_value=Path(d)), \
             patch.dict(os.environ, {}, clear=True):
            legacy=Path(d)/'Library'/'Application Support'/'SoriNote'
            (legacy/'models').mkdir(parents=True)
            (legacy/'models'/'keep').write_bytes(b'model')
            atomic_json(legacy/'settings.json', {'language':'en'})
            self.assertEqual(ensure_dirs(), legacy)
            self.assertEqual(read_json(data_home()/'settings.json'), {'language':'en'})
            self.assertEqual((data_home()/'models'/'keep').read_bytes(), b'model')
            self.assertFalse((legacy.parent/'SoriTaker').exists())

    def test_fresh_data_and_existing_new_data_use_new_name(self):
        with tempfile.TemporaryDirectory() as d, patch('core.Path.home', return_value=Path(d)), \
             patch.dict(os.environ, {}, clear=True):
            expected=Path(d)/'Library'/'Application Support'/'SoriTaker'
            self.assertEqual(ensure_dirs(), expected)
            (expected.parent/'SoriNote').mkdir()
            self.assertEqual(data_home(), expected)

    def test_data_override_supports_legacy_name_and_prefers_new_name(self):
        with patch.dict(os.environ, {'SORINOTE_DATA_DIR':'/legacy'}, clear=True):
            self.assertEqual(data_home(), Path('/legacy'))
            os.environ['SORITAKER_DATA_DIR']='/current'
            self.assertEqual(data_home(), Path('/current'))

    def test_time_rounding_rollover(self):
        self.assertEqual(timestamp(59.9996, True), '00:01:00,000')
        self.assertEqual(timestamp(3600, True), '01:00:00,000')
        self.assertEqual(timestamp(-3, True), '00:00:00,000')

    def test_speaker_change_splits_sentence_without_losing_words(self):
        segments = [dict(start=0, end=4, text='안녕하세요. Hello there.', words=[
            dict(start=0, end=1, word='안녕하세요.'), dict(start=2, end=3, word=' Hello'), dict(start=3, end=4, word=' there.')])]
        turns = [dict(start=0, end=1.2, speaker='A'), dict(start=1.9, end=4, speaker='B')]
        result = label_segments(segments, turns)
        self.assertEqual([s['speaker'] for s in result], ['A', 'B'])
        self.assertEqual(result[1]['text'], 'Hello there.')
        self.assertEqual(result[1]['start'], 2)

    def test_distant_silence_not_assigned_to_wrong_speaker(self):
        self.assertEqual(speaker_at(10, 12, [dict(start=0, end=3, speaker='A')]), 'Unassigned')

    def test_incomplete_word_alignment_preserves_full_text(self):
        raw = [dict(start=0, end=4, text='중요한 전문용어 Thermococcus', words=[dict(start=0, end=1, word='중요한')])]
        result = label_segments(raw, [dict(start=0, end=5, speaker='A')])
        self.assertEqual(result[0]['text'], raw[0]['text'])

    def test_export_uses_edits_and_names_not_original_words(self):
        note = dict(title='연구 회의', speaker_names={'A': '발표자 김'}, segments=[
            dict(start=0.1, end=2.2, text='수정한 결과', speaker='A', words=[dict(word='원본')]),
            dict(start=2.3, end=3, text='   ', speaker='A')])
        with tempfile.TemporaryDirectory() as d:
            for kind in ['txt', 'srt', 'vtt', 'json']:
                path = Path(d) / ('out.' + kind)
                export_note(note, path, kind)
                text = path.read_text()
                self.assertIn('수정한 결과', text)
                if kind != 'json':
                    self.assertNotIn('원본', text)
                    self.assertIn('발표자 김', text)
            self.assertIn('00:00:00,100 --> 00:00:02,200', (Path(d)/'out.srt').read_text())
            self.assertTrue((Path(d)/'out.vtt').read_text().startswith('WEBVTT\n'))

    def test_storage_recovers_prior_data_after_failed_write(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'note.json'
            atomic_json(target, {'title': '기존 기록'})
            with self.assertRaises(ValueError):
                atomic_json(target, {'value': float('nan')})
            self.assertEqual(read_json(target)['title'], '기존 기록')
            self.assertEqual(list(Path(d).glob('*.tmp')), [])

    def test_rerun_creates_new_identity(self):
        self.assertNotEqual(new_note('/tmp/a.wav', 'turbo', 'ko', False, 0, '')['id'],
                            new_note('/tmp/a.wav', 'turbo', 'ko', False, 0, '')['id'])

    def test_traversal_note_id_rejected(self):
        with self.assertRaises(ValueError):
            note_dir({'id': '../../bad'})


if __name__ == '__main__':
    unittest.main()
