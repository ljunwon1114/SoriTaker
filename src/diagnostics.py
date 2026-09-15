"""Runs inside the built Mac app, before the installer replaces an existing app."""
from pathlib import Path
import importlib.metadata
import platform
import tempfile
import traceback
from core import atomic_json


def self_check(destination):
    result = {'ok': False, 'platform': platform.platform(), 'architecture': platform.machine(), 'checks': {}}
    check = result['checks']
    try:
        import numpy as np
        import soundfile as sf
        import sounddevice
        from PySide6 import QtCore, QtMultimedia
        import sherpa_onnx
        from worker import ffmpeg_path, transcribe, Reporter, diarize
        from models import ready, model_dir
        import mlx.core as mx
        mx.eval(mx.array([1.0, 2.0]) + 1)
        check['mlx_compute'] = True
        check['ffmpeg'] = Path(ffmpeg_path()).is_file()
        if not check['ffmpeg']:
            raise RuntimeError('The FFmpeg executable is missing from the app bundle.')
        check['gui_audio_diarization_imports'] = True
        check['microphone_permission'] = 'Allow microphone access in macOS when you start your first recording.'
        check['versions'] = {}
        for package in ['mlx', 'mlx-whisper', 'PySide6', 'sherpa-onnx']:
            try:
                check['versions'][package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                check['versions'][package] = 'bundled'
        # Exercise tokenization + model loading from the actual bundled application.
        # No microphone and no external audio are used by this check.
        with tempfile.TemporaryDirectory(prefix='soritaker-check-') as temporary:
            reporter = Reporter(Path(temporary))
            if ready('turbo'):
                import mlx_whisper
                raw = mlx_whisper.transcribe(np.zeros(16000, dtype=np.float32),
                    path_or_hf_repo=str(model_dir('turbo')), language='en', verbose=None,
                    condition_on_previous_text=False, fp16=True, temperature=0.0)
                check['bundled_turbo_load'] = isinstance(raw.get('segments'), list)
            else:
                check['bundled_turbo_load'] = 'Pending: download the model before checking model loading.'
            if ready('diarization'):
                turns = diarize(np.zeros(16000, dtype=np.float32), 1, reporter)
                check['bundled_speaker_models_load'] = isinstance(turns, list)
            else:
                check['bundled_speaker_models_load'] = 'Pending: download the models before checking model loading.'
        result['ok'] = True
    except Exception as exc:
        result['error'] = str(exc)
        result['traceback'] = traceback.format_exc()
    atomic_json(Path(destination), result)
    return 0 if result['ok'] else 1
