"""Audio to text. ElevenLabs Scribe when ELEVENLABS_API_KEY works; if there is no key or it is
rejected, a local Whisper (faster-whisper, no key, no cost) on the same machine."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v2"
MAX_BYTES = 100 * 1024 * 1024  # a single upload; longer audio is cut to mono 16 kHz before sending


class TranscribeError(RuntimeError):
    pass


def _key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if key:
        return key
    # Optional env file shared with other tools (config.json → personal.extra_env_file). Only that one line is read.
    import radar
    extra = radar.personal("extra_env_file")
    if not extra:
        raise TranscribeError("Falta ELEVENLABS_API_KEY (variable de entorno)")
    env = Path(extra).expanduser()
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    raise TranscribeError("Falta ELEVENLABS_API_KEY (variable de entorno o personal.extra_env_file)")


def _shrink(path: Path, out_dir: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TranscribeError(f"{path.name} pesa demasiado y no hay ffmpeg para achicarlo")
    out = out_dir / "audio.mp3"
    done = subprocess.run([ffmpeg, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(out)],
                          capture_output=True, timeout=600)
    if done.returncode != 0 or not out.exists():
        raise TranscribeError(f"ffmpeg no pudo leer {path.name}")
    return out


def _local(src: Path, language: str | None) -> str:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise TranscribeError("No hay key de ElevenLabs valida y falta faster-whisper (pip install faster-whisper)") from exc
    model = WhisperModel(os.environ.get("WORKWORM_WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(src), language=language, vad_filter=True)
    text = " ".join(seg.text.strip() for seg in segments).strip()
    if not text:
        raise TranscribeError("El audio no tiene voz que se pueda transcribir")
    return text


def transcribe(path: str | Path, language: str | None = None, timeout: int = 600) -> str:
    """Text of an audio or video file. Raises TranscribeError with a readable reason."""
    src = Path(path)
    if not src.is_file():
        raise TranscribeError(f"No existe el archivo: {src}")
    try:
        return _scribe(src, language, timeout)
    except TranscribeError as exc:
        if "401" in str(exc) or "403" in str(exc) or "Falta ELEVENLABS" in str(exc):
            return _local(src, language)
        raise


def _scribe(src: Path, language: str | None, timeout: int) -> str:
    key = _key()
    with tempfile.TemporaryDirectory() as tmp:
        upload = src if src.stat().st_size <= MAX_BYTES else _shrink(src, Path(tmp))
        data = {"model_id": MODEL}
        if language:
            data["language_code"] = language
        try:
            with upload.open("rb") as stream:
                resp = requests.post(URL, headers={"xi-api-key": key}, data=data,
                                     files={"file": (upload.name, stream)}, timeout=timeout)
        except requests.RequestException as exc:
            raise TranscribeError(f"No pude llegar a ElevenLabs: {exc}") from exc
    if resp.status_code != 200:
        raise TranscribeError(f"ElevenLabs respondió {resp.status_code}: {resp.text[:200]}")
    text = (resp.json().get("text") or "").strip()
    if not text:
        raise TranscribeError("El audio no tiene voz que se pueda transcribir")
    return text


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        raise SystemExit("uso: python transcribe.py archivo [idioma]")
    print(transcribe(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
