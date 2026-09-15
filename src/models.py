"""All network access is restricted to explicit model installation."""
from __future__ import annotations

import hashlib
import json
import os
import tarfile
import urllib.request
from pathlib import Path

from core import APP_NAME, VERSION, ensure_dirs, atomic_json, read_json

MODELS = {
    "turbo": {"name": "Fast · Large v3 Turbo", "repo": "mlx-community/whisper-large-v3-turbo", "size": "1.6 GB"},
    "large": {"name": "Accurate · Large v3", "repo": "mlx-community/whisper-large-v3-mlx", "size": "3.1 GB"},
    "diarization": {"name": "Speaker labels", "size": "47 MB"},
}
BASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
SEG_URL = BASE + "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
EMB_URL = BASE + "speaker-recongition-models/nemo_en_titanet_small.onnx"


def clear_legacy_offline_flags(environment=None):
    """Remove flags inherited from older releases without changing OS settings.

    Local transcription uses installed model paths; it does not need a global
    network switch. Clear stale flags before the Hub client caches them at import.
    """
    env = os.environ if environment is None else environment
    env.pop('HF_HUB_OFFLINE', None)
    env.pop('TRANSFORMERS_OFFLINE', None)
    env['HF_HUB_DISABLE_TELEMETRY'] = '1'
    env['DO_NOT_TRACK'] = '1'
    return env


def model_dir(key: str) -> Path:
    if key not in MODELS:
        raise ValueError("Unknown model")
    return ensure_dirs() / "models" / key


def ready(key: str) -> bool:
    path = model_dir(key)
    manifest = read_json(path / "installed.json")
    if not manifest:
        return False
    return all((path / f).is_file() and (path / f).stat().st_size == size
               for f, size in manifest.get("files", {}).items()) and bool(manifest.get("files"))


def fetch(url: str, dest: Path, progress) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
    with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
        total = int(response.headers.get("Content-Length", 0))
        done = 0
        while block := response.read(1024 * 1024):
            out.write(block)
            done += len(block)
            progress(f"Downloading model · {done / 1e6:.0f} MB" + (f" / {total / 1e6:.0f} MB" if total else ""))
    partial.replace(dest)


def install(key: str, progress=print) -> None:
    # This function is called only for explicitly requested model installation.
    # Clear old flags before importing huggingface_hub, which caches them.
    clear_legacy_offline_flags()
    if ready(key):
        progress(f"{MODELS[key]['name']} ready")
        return
    path = model_dir(key)
    path.mkdir(parents=True, exist_ok=True)
    if key == "diarization":
        archive = path / "segmentation.tar.bz2"
        progress("Downloading speaker models")
        fetch(SEG_URL, archive, progress)
        # Extract only the two explicitly allowed regular files, never archive paths.
        with tarfile.open(archive, "r:bz2") as tar:
            for src, dst in (("model.onnx", "segmentation.onnx"), ("LICENSE", "segmentation-LICENSE")):
                member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == src), None)
                if not member:
                    raise RuntimeError(f"Missing file in model archive: {src}")
                with tar.extractfile(member) as inp, (path / dst).open("wb") as out:
                    while block := inp.read(1024 * 1024):
                        out.write(block)
        archive.unlink()
        fetch(EMB_URL, path / "embedding.onnx", progress)
        files = ["segmentation.onnx", "embedding.onnx", "segmentation-LICENSE"]
        origin = {"segmentation": SEG_URL, "embedding": EMB_URL}
    else:
        from huggingface_hub import snapshot_download, HfApi
        # Resolve a concrete commit and retain it with the installation record.
        repo = MODELS[key]["repo"]
        progress(f"{MODELS[key]['name']} downloading · {MODELS[key]['size']}\nInternet is used only for model installation.")
        revision = HfApi().model_info(repo).sha
        snapshot_download(repo_id=repo, revision=revision, local_dir=str(path),
                          allow_patterns=["*.json", "*.safetensors", "*.npz", "README.md", "LICENSE*"],
                          max_workers=2)
        files = [p.name for p in path.iterdir() if p.is_file() and p.name != "installed.json"]
        if not (path / "config.json").is_file() or not any((path / f).suffix in (".safetensors", ".npz") for f in files):
            raise RuntimeError("Incomplete model download. Download the model again in Options.")
        origin = {"repo": repo, "revision": revision}
    # A completed manifest is the commit point; interrupted downloads never look installed.
    atomic_json(path / "installed.json", {
        "origin": origin,
        "files": {f: (path / f).stat().st_size for f in files},
    })
    progress(f"{MODELS[key]['name']} ready")
