"""Storage and transcript operations. No networking or model imports here."""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

APP_NAME = "SoriTaker"
VERSION = "0.4.2"


def data_home() -> Path:
    override = os.environ.get("SORITAKER_DATA_DIR") or os.environ.get("SORINOTE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    support = Path.home() / "Library" / "Application Support"
    current, legacy = support / APP_NAME, support / "SoriNote"
    # Keep existing recordings, settings, models and runtime at their original paths.
    return legacy if not current.exists() and legacy.is_dir() else current


def ensure_dirs() -> Path:
    root = data_home()
    for name in ("notes", "models", "jobs", "logs", "recordings"):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def new_note(source: str, model: str, language: str, diarize: bool, speakers: int, prompt: str) -> dict:
    return {
        "schema": 1, "id": uuid.uuid4().hex, "title": Path(source).stem,
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(Path(source).resolve()), "model": model, "language": language,
        "diarize": diarize, "speaker_count": speakers, "prompt": prompt,
        "status": "ready", "segments": [], "speaker_names": {}, "warnings": [],
    }


def note_dir(note: dict) -> Path:
    note_id = note["id"]
    if not re.fullmatch(r"[a-f0-9]{32}", note_id):
        raise ValueError("Invalid note identifier")
    return ensure_dirs() / "notes" / note_id


def save_note(note: dict) -> None:
    atomic_json(note_dir(note) / "note.json", note)


def all_notes() -> list[dict]:
    notes = []
    for path in (ensure_dirs() / "notes").glob("*/note.json"):
        note = read_json(path)
        if isinstance(note, dict) and note.get("id") == path.parent.name:
            notes.append(note)
    return sorted(notes, key=lambda x: x.get("created", ""), reverse=True)


def timestamp(seconds: float, srt: bool = False) -> str:
    value = max(0, round(float(seconds) * 1000))
    h, rem = divmod(value, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    base = f"{h:02}:{m:02}:{s:02}"
    return f"{base},{ms:03}" if srt else base


def display_speaker(note: dict, key: str) -> str:
    return note.get("speaker_names", {}).get(key, key)


def transcript_text(note: dict) -> str:
    lines = [note.get("title", ""), ""]
    for s in sentence_segments(note.get("segments", [])):
        speaker = display_speaker(note, s.get("speaker", ""))
        label = f"  {speaker}" if speaker else ""
        lines.append(f"[{timestamp(s['start'])}]{label}\n{s['text'].strip()}\n")
    return "\n".join(lines).rstrip() + "\n"


def sentence_end(value: str) -> bool:
    """A conservative punctuation boundary, not a grammatical rewrite."""
    value = value.rstrip().rstrip('\"\'”’)]}')
    if not value or not re.search(r'[\w\uac00-\ud7a3]', value):
        return False
    if value.endswith(('?', '!', '。', '？', '！')):
        return True
    if not value.endswith('.') or value.endswith('..'):
        return False
    last = value.split()[-1].lower()
    if last in {'dr.', 'prof.', 'mr.', 'mrs.', 'ms.', 'vs.', 'fig.', 'figs.', 'no.', 'nos.', 'e.g.', 'i.e.', 'al.'}:
        return False
    # Initials, abbreviated species names, dotted acronyms, and bare numbered items.
    return not re.fullmatch(r'(?:[a-z]\.)+|\d+\.', last)


def sentence_segments(segments: list[dict]) -> list[dict]:
    """Readable TXT rows; preserve recognized words, speaker turns and order.

    Join decoder fragments until sentence punctuation. Never join a different
    speaker or a long pause. Word alignment supplies sentence-start timestamps
    only when it accounts for the entire current (possibly edited) text.
    """
    result, current = [], None
    norm = lambda value: re.sub(r'\s+', '', value)
    for segment in segments:
        body = segment.get('text', '').strip()
        if not body:
            continue
        words = segment.get('words') or []
        aligned = bool(words) and all(
            isinstance(w.get('word'), str) and norm(w['word']) and
            isinstance(w.get('start'), (int, float)) and isinstance(w.get('end'), (int, float)) and
            segment['start'] <= w['start'] <= w['end'] <= segment['end'] + 0.05
            for w in words)
        aligned = aligned and norm(''.join(w['word'] for w in words)) == norm(body)
        if aligned:
            aligned = all(a['start'] <= b['start'] for a,b in zip(words,words[1:]))
        if aligned:
            positions = [i for i,c in enumerate(body) if not c.isspace()]
            units, count, previous = [], 0, 0
            for w in words:
                count += len(norm(w['word']))
                end = positions[count-1]+1
                units.append((body[previous:end], float(w['start']), float(w['end'])))
                previous = end
        else:
            # Retain edited text and the source segment time if alignment is incomplete.
            units = [(body, float(segment['start']), float(segment['end']))]
        for index,(part,start,end) in enumerate(units):
            speaker = segment.get('speaker', '')
            if current and (current['speaker'] != speaker or start-current['end'] > 8 or
                            end-current['start'] > 60 or len(current['text']) > 800):
                result.append(current)
                current = None
            if current is None:
                current = dict(start=start, end=end, text=part.lstrip(), speaker=speaker)
            else:
                # Inside a segment retain the exact original whitespace, including Korean.
                separator = ' ' if index == 0 and not part.startswith((' ', '\n', '\t')) else ''
                current['text'] += separator + part
                current['end'] = max(current['end'], end)
            if sentence_end(current['text']):
                result.append(current)
                current = None
    if current:
        result.append(current)
    return result


def export_note(note: dict, path: Path, kind: str) -> None:
    if kind == "json":
        atomic_json(path, note)
        return
    if kind in ("srt", "vtt"):
        blocks = []
        for s in note.get("segments", []):
            text = s.get("text", "").strip()
            if not text:
                continue
            speaker = display_speaker(note, s.get("speaker", ""))
            text = (f"[{speaker}] " if speaker else "") + text
            start = timestamp(s["start"], True)
            end = timestamp(max(s["start"] + 0.001, s["end"]), True)
            if kind == "vtt":
                start, end = start.replace(",", "."), end.replace(",", ".")
            blocks.append(f"{len(blocks) + 1}\n{start} --> {end}\n{text}")
        body = ("WEBVTT\n\n" if kind == "vtt" else "") + "\n\n".join(blocks) + "\n"
    elif kind == "txt":
        body = transcript_text(note)
    else:
        raise ValueError("Unsupported export format")
    path.write_text(body, encoding="utf-8")


def speaker_at(start: float, end: float, turns: list[dict]) -> str:
    scores: dict[str, float] = {}
    for turn in turns:
        overlap = max(0.0, min(end, turn["end"]) - max(start, turn["start"]))
        if overlap:
            scores[turn["speaker"]] = scores.get(turn["speaker"], 0.0) + overlap
    if scores:
        return max(scores, key=scores.get)
    # Silence and uncertain alignments must not be assigned to a distant speaker.
    midpoint = (start + end) / 2
    nearest = min(turns, key=lambda t: min(abs(midpoint - t["start"]), abs(midpoint - t["end"])), default=None)
    if nearest and min(abs(midpoint - nearest["start"]), abs(midpoint - nearest["end"])) <= 0.4:
        return nearest["speaker"]
    return "Unassigned"


def label_segments(segments: list[dict], turns: list[dict]) -> list[dict]:
    """Assign words by time overlap; split a sentence at a speaker change."""
    labeled = []
    for segment in segments:
        words = segment.get("words") or []
        usable = [w for w in words if w.get("end", 0) > w.get("start", 0) and w.get("word", "").strip()]
        # Preserve the engine's complete text when word alignment loses text.
        norm = lambda s: re.sub(r"\s+", "", s)
        if not usable or norm("".join(w["word"] for w in usable)) != norm(segment["text"]):
            labeled.append({**segment, "speaker": speaker_at(segment["start"], segment["end"], turns)})
            continue
        groups = []
        for w in usable:
            speaker = speaker_at(w["start"], w["end"], turns)
            if groups and groups[-1]["speaker"] == speaker:
                groups[-1]["words"].append(w)
                groups[-1]["end"] = w["end"]
                groups[-1]["text"] += w["word"]
            else:
                groups.append({"start": w["start"], "end": w["end"], "speaker": speaker,
                               "text": w["word"], "words": [w]})
        for group in groups:
            group["text"] = group["text"].strip()
        labeled.extend(groups)
    return labeled


def save_outputs(note: dict, destination: Path, basename: str) -> list[str]:
    """Publish audio + TXT without replacing existing files, even on name collisions."""
    import shutil
    if not basename or basename in ('.', '..') or re.search(r'[/\\\x00-\x1f]', basename):
        raise ValueError('Invalid output file name')
    destination.mkdir(parents=True, exist_ok=True)
    original = Path(note['original'])
    # Video imports produce a WAV audio file rather than a duplicate video.
    source = original if original.suffix.lower() in ('.wav', '.m4a', '.mp3', '.flac', '.aac', '.ogg', '.aif', '.aiff', '.opus', '.wma') else Path(note['audio'])
    audio_suffix = source.suffix.lower() or '.wav'
    index = 0
    while True:
        name = basename if index == 0 else f'{basename} ({index + 1})'
        audio_path, text_path = destination / (name + audio_suffix), destination / (name + '.txt')
        audio_fd = text_fd = None
        try:
            audio_fd = os.open(audio_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            text_fd = os.open(text_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if audio_fd is not None:
                os.close(audio_fd)
                audio_path.unlink()
            index += 1
        except Exception:
            if audio_fd is not None:
                os.close(audio_fd)
                audio_path.unlink()
            raise
    try:
        audio_stream = os.fdopen(audio_fd, 'wb')
        audio_fd = None
        with audio_stream as target, source.open('rb') as inp:
            shutil.copyfileobj(inp, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        with os.fdopen(text_fd, 'w', encoding='utf-8') as target:
            text_fd = None
            target.write(transcript_text(note))
            target.flush()
            os.fsync(target.fileno())
        return [str(audio_path), str(text_path)]
    except Exception:
        for fd in (audio_fd, text_fd):
            if fd is not None:
                os.close(fd)
        # These are new files reserved by this operation, never pre-existing files.
        audio_path.unlink(missing_ok=True)
        text_path.unlink(missing_ok=True)
        raise


def save_audio_output(source: Path, destination: Path, basename: str) -> list[str]:
    """Copy the recording unchanged. No model, decoder, or transcript is needed."""
    import shutil
    if not basename or basename in ('.', '..') or re.search(r'[/\\\x00-\x1f]', basename):
        raise ValueError('Invalid output file name')
    if not source.is_file():
        raise FileNotFoundError('The original file is missing. Import it again.')
    destination.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() or '.wav'
    index = 0
    while True:
        name = basename if index == 0 else f'{basename} ({index + 1})'
        target_path = destination / (name + suffix)
        try:
            fd = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            index += 1
    try:
        with os.fdopen(fd, 'wb') as target, source.open('rb') as original:
            shutil.copyfileobj(original, target, 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        return [str(target_path)]
    except Exception:
        target_path.unlink(missing_ok=True)
        raise


def discard_pending(source: str, keep_job: Path | None = None) -> None:
    """Remove this source's app-owned work only; never follow saved output paths.

    Also finds failed attempts after relaunch. External imported originals and
    unrelated recordings are preserved. Never traverse a symlinked work folder.
    """
    import shutil
    root = ensure_dirs().resolve()
    current = Path(source).resolve()
    for category, metadata in (('notes', 'note.json'), ('jobs', 'job.json')):
        for folder in (root / category).iterdir():
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'[a-f0-9]{32}', folder.name):
                continue
            if keep_job is not None and folder == keep_job.resolve():
                continue
            info = read_json(folder / metadata, {})
            if not isinstance(info, dict):
                continue
            if category == 'jobs':
                if info.get('queue_managed'):
                    continue  # The queue owns its history and input recordings.
                if info.get('kind') not in ('transcribe', 'save_audio'):
                    continue
                info = info.get('note', {}) if info.get('kind') == 'transcribe' else info
            linked = info.get('source') if isinstance(info, dict) else None
            if isinstance(linked, str) and Path(linked).resolve() == current:
                shutil.rmtree(folder)
    # Resolve before checking ownership so an imported symlink cannot delete its target.
    original = Path(source)
    if not original.is_symlink() and current.parent == root / 'recordings':
        current.unlink(missing_ok=True)
