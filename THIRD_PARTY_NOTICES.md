# Third-party components

SoriTaker's original source is provided under the MIT License. Dependencies and model weights retain their own licenses. The installer obtains components from their maintainers; the ZIP does not bundle model weights.

- MLX and mlx-whisper: Apple / MLX contributors, MIT. https://github.com/ml-explore/mlx and https://github.com/ml-explore/mlx-examples/tree/main/whisper
- Whisper original models: OpenAI, MIT. https://github.com/openai/whisper
- Converted Whisper checkpoints: MLX Community. https://huggingface.co/mlx-community/whisper-large-v3-turbo and https://huggingface.co/mlx-community/whisper-large-v3-mlx
- PySide6 / Qt: The Qt Company, LGPLv3/GPLv3 or commercial, depending on component. This application dynamically bundles PySide6. Original source and replacement/rebuild instructions are included in the package. https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://www.qt.io/licensing/open-source-lgpl-obligations
- sherpa-onnx: Apache-2.0. https://github.com/k2-fsa/sherpa-onnx
- pyannote segmentation-3.0 ONNX: the upstream sherpa distribution includes its LICENSE, retained beside the installed model. https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html
- NeMo TitaNet small speaker embedding: NVIDIA NeMo; check model-specific terms before redistribution. https://huggingface.co/nvidia/speakerverification_en_titanet_small and https://github.com/NVIDIA/NeMo
- sounddevice and soundfile: MIT / BSD-3-Clause; PortAudio and libsndfile retain their respective licenses. https://python-sounddevice.readthedocs.io/ and https://python-soundfile.readthedocs.io/
- imageio-ffmpeg: BSD-2-Clause. Bundled FFmpeg has separate LGPL/GPL terms depending on its build, inspect the binary's `-L` / `-buildconf` before redistribution. https://github.com/imageio/imageio-ffmpeg
- NumPy: BSD-3-Clause. https://numpy.org/
- Hugging Face Hub: Apache-2.0. https://github.com/huggingface/huggingface_hub
- uv: MIT / Apache-2.0. https://github.com/astral-sh/uv
- PyInstaller: GPL-2.0-or-later with bootloader exception. https://pyinstaller.org/

This is a personal-use source installer. It is not a signed or notarized commercial distribution. Rebuilding is possible by editing the source and running Install.command on an Apple Silicon Mac. Dependency changes must also be reflected in requirements-macos.lock, which the installer uses. Model download origins and resolved Hugging Face revisions are retained in models/*/installed.json.
