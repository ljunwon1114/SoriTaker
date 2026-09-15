"""Isolated processing process. Cancellation releases all model/GPU memory."""
from __future__ import annotations
import contextlib
import io
import os
import re
import shutil
import subprocess
import time
import traceback
from pathlib import Path
from core import (ensure_dirs, atomic_json, read_json, note_dir, save_note, label_segments,
                  save_outputs, save_audio_output, discard_pending)
from models import model_dir, ready, install, clear_legacy_offline_flags


class Reporter:
    def __init__(self, job_dir):
        self.path = job_dir / 'status.json'
        self.last = 0.0

    def update(self, message, progress=None, state='running', **extra):
        value = dict(message=message, progress=progress, state=state, **extra)
        if state == 'complete' and read_json(self.path.parent/'job.json', {}).get('queue_managed'):
            atomic_json(self.path.parent/'result.json', value)
        atomic_json(self.path, value)


class ASRProgress(io.TextIOBase):
    def __init__(self, reporter):
        self.reporter = reporter

    def write(self, text):
        match = re.search(r'(\d+)%', text)
        if match and time.monotonic() - self.reporter.last > 0.5:
            self.reporter.last = time.monotonic()
            value = int(match.group(1))
            self.reporter.update(f'Transcribing · {value}%', value)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def ffmpeg_path():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def convert_audio(source, target):
    partial = target.with_name('audio-converting.wav')
    result = subprocess.run([
        ffmpeg_path(), '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
        '-protocol_whitelist', 'file,pipe', '-i', str(source), '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(partial),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise RuntimeError('Could not read the audio file.\n' + result.stderr[-1500:])
    partial.replace(target)


def transcribe(audio, note, reporter):
    import mlx_whisper
    options = dict(
        path_or_hf_repo=str(model_dir(note['model'])),
        language=None if note['language'] == 'auto' else note['language'],
        task='transcribe', fp16=True, word_timestamps=True,
        condition_on_previous_text=False, verbose=False,
        temperature=(0.0, 0.2, 0.4, 0.6), no_speech_threshold=0.6,
        logprob_threshold=-1.0, compression_ratio_threshold=2.4,
        initial_prompt=note.get('prompt', '').strip()[:800] or None,
        hallucination_silence_threshold=2.0,
    )
    with contextlib.redirect_stderr(ASRProgress(reporter)):
        return mlx_whisper.transcribe(audio, **options)


def diarize(audio, speakers, reporter):
    import sherpa_onnx
    path = model_dir('diarization')
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(path / 'segmentation.onnx')),
            num_threads=4, provider='cpu',
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(path / 'embedding.onnx'), num_threads=4, provider='cpu',
        ),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=speakers if speakers > 0 else -1, threshold=0.5),
        min_duration_on=0.3, min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError('Speaker model files are invalid. Download the speaker models again in Options.')
    engine = sherpa_onnx.OfflineSpeakerDiarization(config)

    def callback(done, total):
        if time.monotonic() - reporter.last > 0.5:
            reporter.last = time.monotonic()
            percent = round(100 * done / max(1, total))
            reporter.update(f'Identifying speakers · {percent}%', percent)
        return 0

    output = engine.process(audio, callback=callback).sort_by_start_time()
    names, turns = {}, []
    for turn in output:
        key = names.setdefault(turn.speaker, f'Speaker {len(names) + 1}')
        turns.append(dict(start=float(turn.start), end=float(turn.end), speaker=key))
    return turns


def run_job(spec_path):
    spec_file = Path(spec_path)
    spec = read_json(spec_file, {})
    if not spec.get('queue_managed'):
        return _run_job(spec_path)
    reporter = Reporter(spec_file.parent)
    try:
        # Also serializes a worker left alive after an unexpected GUI shutdown.
        # No model is imported until the previous worker releases this OS lock.
        import fcntl
        with (ensure_dirs()/'processing.lock').open('a') as lock:
            reporter.update('Waiting for the previous worker…')
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = read_json(spec_file.parent/'result.json', {})
            if result.get('state') == 'complete':
                atomic_json(reporter.path, result)
                return 0
            return _run_job(spec_path)
    except Exception as exc:
        result = read_json(spec_file.parent/'result.json', {})
        if result.get('state') == 'complete':
            atomic_json(reporter.path, result)
            return 0
        reporter.update(str(exc), state='error')
        return 1


def _run_job(spec_path):
    spec_file = Path(spec_path)
    reporter = Reporter(spec_file.parent)
    spec = read_json(spec_file)
    if not spec:
        reporter.update('Could not read the job settings.', state='error')
        return 1
    try:
        # Covers direct --worker calls as well as GUI-launched fresh processes.
        clear_legacy_offline_flags()
        if spec['kind'] == 'download':
            install(spec['model'], lambda message: reporter.update(message))
            reporter.update('Model is ready.', 100, state='complete')
            return 0
        if spec['kind'] == 'save_audio':
            source = Path(spec['source'])
            reporter.update('Saving audio only…')
            # Deliberately before model checks and audio conversion.
            saved_paths = save_audio_output(source, Path(spec['export_folder']), spec['export_name'])
            reporter.update('Audio saved. No transcript was created.', 100, state='complete',
                            saved_paths=saved_paths)
            if not spec.get('queue_managed'):
                try:
                    discard_pending(str(source), keep_job=spec_file.parent)
                except OSError:
                    pass  # Saving succeeded; remaining temporary data is recoverable.
            return 0
        if spec['kind'] != 'transcribe':
            raise ValueError('Unknown job type')
        note = spec['note']
        if not ready(note['model']):
            raise RuntimeError('Download the selected transcription model in Options first.')
        if note['diarize'] and note['speaker_count'] != 1 and not ready('diarization'):
            raise RuntimeError('Download the speaker models in Options first.')
        folder = note_dir(note)
        folder.mkdir(parents=True, exist_ok=True)
        source = Path(note['source'])
        if not source.is_file():
            raise RuntimeError('The original file is missing. Import it again.')
        reporter.update('Preserving your original audio…')
        owned = folder / ('original' + source.suffix.lower())
        if source.resolve() != owned.resolve():
            shutil.copy2(source, owned)
        note['original'] = str(owned)
        note['status'] = 'processing'
        save_note(note)
        audio_path = folder / 'audio.wav'
        reporter.update('Preparing audio…')
        convert_audio(owned, audio_path)
        note['audio'] = str(audio_path)
        import numpy as np
        import soundfile as sf
        audio, sample_rate = sf.read(str(audio_path), dtype='float32')
        if sample_rate != 16000 or audio.ndim != 1:
            raise RuntimeError('Audio conversion produced an invalid format.')
        note['duration'] = len(audio) / sample_rate
        if len(audio) < sample_rate / 2:
            raise RuntimeError('The recording is too short. Record at least one second.')
        started = time.monotonic()
        reporter.update('Loading the transcription model…')
        if float(np.max(np.abs(audio))) < 0.0001:
            raw = dict(text='', segments=[], language=note['language'])
        else:
            raw = transcribe(audio, note, reporter)
        atomic_json(folder / 'recognition-original.json', raw)
        note['detected_language'] = raw.get('language', note['language'])
        note['segments'] = [dict(
            start=float(s['start']), end=float(s['end']), text=s['text'].strip(),
            words=s.get('words', []), speaker='',
        ) for s in raw['segments'] if s.get('text', '').strip() and s['end'] > s['start']]
        note['status'] = 'transcribed'
        save_note(note)
        if not note['segments']:
            note['warnings'].append('No speech was recognized. Check your microphone and recording level.')
        elif note['detected_language'] not in ('ko', 'en'):
            note['warnings'].append('A language other than Korean or English was detected. Select the language in Options and try again.')
        if note['diarize'] and note['segments']:
            if note['speaker_count'] == 1:
                for segment in note['segments']:
                    segment['speaker'] = 'Speaker 1'
            else:
                try:
                    # MLX cache cleanup is best-effort, not a diarization prerequisite.
                    try:
                        import gc
                        import importlib
                        module = importlib.import_module('mlx_whisper.transcribe')
                        holder = getattr(module, 'ModelHolder', None)
                        if holder is not None:
                            holder.model = None
                        gc.collect()
                        import mlx.core as mx
                        if hasattr(mx, 'clear_cache'):
                            mx.clear_cache()
                    except Exception:
                        pass
                    reporter.update('Loading speaker models…')
                    turns = diarize(audio, note['speaker_count'], reporter)
                    atomic_json(folder / 'speaker-turns.json', turns)
                    if turns:
                        note['segments'] = label_segments(note['segments'], turns)
                    else:
                        note['warnings'].append('Could not identify speakers. The transcript has been preserved.')
                except Exception as exc:
                    note['warnings'].append(f'Speaker identification did not finish. The transcript has been preserved. ({exc})')
        note['elapsed'] = round(time.monotonic() - started, 1)
        note['status'] = 'complete'
        save_note(note)
        saved_paths = []
        if spec.get('export_folder'):
            reporter.update('Saving audio and transcript…')
            saved_paths = save_outputs(note, Path(spec['export_folder']), spec['export_name'])
        reporter.update('Audio and transcript saved.' if saved_paths else 'Transcript is ready.',
                        100, state='complete', note_id=note['id'], saved_paths=saved_paths,
                        warnings=note.get('warnings', []))
        if saved_paths and not spec.get('queue_managed'):
            # Clean current and previous attempts only after both files are committed.
            try:
                discard_pending(str(source), keep_job=spec_file.parent)
            except OSError:
                pass
        return 0
    except Exception as exc:
        if (read_json(reporter.path, {}).get('state') == 'complete' or
                read_json(spec_file.parent/'result.json', {}).get('state') == 'complete'):
            return 0  # Cancellation during cleanup must not undo a committed save.
        (spec_file.parent / 'error.log').write_text(traceback.format_exc(), encoding='utf-8')
        if spec.get('kind') == 'transcribe':
            note = read_json(note_dir(spec['note']) / 'note.json', spec['note'])
            note['status'] = 'partial' if note.get('segments') else 'error'
            note['error'] = str(exc)
            save_note(note)
        reporter.update(str(exc), state='error')
        return 1


def cancel_on_signal(signum, frame):
    # Unwind copy/write contexts so cancelling a save removes newly created files.
    raise RuntimeError('Cancelled. Your original audio is preserved.')
