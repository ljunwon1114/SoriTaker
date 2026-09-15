import unittest
from copy import deepcopy
from core import sentence_segments, transcript_text


def segment(start, text, speaker='', words=None):
    return dict(start=start, end=start+2, text=text, speaker=speaker, words=words or [])


class SentenceTests(unittest.TestCase):
    def test_fragments_join_to_a_sentence_at_original_start(self):
        source=[segment(0,'We examined'),segment(3,'DNA repair'),segment(6,'in archaea.')]
        before=deepcopy(source)
        rows=sentence_segments(source)
        self.assertEqual([r['text'] for r in rows],['We examined DNA repair in archaea.'])
        self.assertEqual(rows[0]['start'],0)
        self.assertEqual(rows[0]['end'],8)
        self.assertEqual(source,before)

    def test_two_sentences_in_one_aligned_segment_get_word_times(self):
        source=[dict(start=0,end=4,text='DNA breaks. Repair starts.',words=[
            dict(start=0,end=.7,word='DNA'),dict(start=.7,end=1.5,word=' breaks.'),
            dict(start=2,end=2.8,word=' Repair'),dict(start=2.8,end=4,word=' starts.')])]
        rows=sentence_segments(source)
        self.assertEqual([r['text'] for r in rows],['DNA breaks.','Repair starts.'])
        self.assertEqual([r['start'] for r in rows],[0,2])

    def test_korean_word_pieces_keep_original_spacing_and_text(self):
        source=[dict(start=0,end=3,text='복구합니다. 다음 결과입니다.',words=[
            dict(start=0,end=.3,word='복'),dict(start=.3,end=.8,word='구합니다.'),
            dict(start=1,end=1.5,word=' 다음'),dict(start=1.5,end=3,word=' 결과입니다.')])]
        rows=sentence_segments(source)
        self.assertEqual([r['text'] for r in rows],['복구합니다.','다음 결과입니다.'])

    def test_abbreviations_and_decimal_are_not_sentence_breaks(self):
        source=[segment(0,'Dr.'),segment(3,'Kim studied S.'),segment(6,'islandicus at pH 3.1.')]
        rows=sentence_segments(source)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['text'],'Dr. Kim studied S. islandicus at pH 3.1.')

    def test_speaker_changes_and_long_pauses_are_preserved(self):
        rows=sentence_segments([segment(0,'My question','A'),segment(3,'The answer','B'),segment(30,'Later','B')])
        self.assertEqual(len(rows),3)
        self.assertEqual([r['speaker'] for r in rows],['A','B','B'])

    def test_incomplete_alignment_uses_edited_text_without_losing_words(self):
        source=[segment(0,'Keep every scientific term',words=[dict(start=0,end=1,word='Keep')]),
                segment(3,'including Mre11/Rad50.')]
        rows=sentence_segments(source)
        self.assertEqual(rows[0]['text'],'Keep every scientific term including Mre11/Rad50.')

    def test_ellipsis_does_not_split_or_silently_delete_text(self):
        rows=sentence_segments([segment(0,'...repair...'),segment(3,'...continues.')])
        self.assertEqual([r['text'] for r in rows],['...repair... ...continues.'])

    def test_txt_uses_sentences_and_single_speaker_has_no_labels(self):
        note=dict(title='Lecture',segments=[segment(0,'We examined'),segment(3,'DNA repair.')])
        text=transcript_text(note)
        self.assertEqual(text,'Lecture\n\n[00:00:00]\nWe examined DNA repair.\n')
        self.assertNotIn('Speaker',text)


if __name__=='__main__':
    unittest.main()
