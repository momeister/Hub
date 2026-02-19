"""
skills/audio/skill.py — Voicebox TTS + STT Integration
=======================================================
TTS: Voicebox REST API (Qwen3-TTS with voice cloning)
STT: Voicebox REST API (Whisper-large)

Voicebox server: configurable via VOICEBOX_URL env var.
Default: http://127.0.0.1:17493

On-demand lifecycle: starts voicebox-server.exe automatically when needed,
shuts down when no longer in use (same pattern as ComfyUI).

Features:
  - Text-to-speech with voice cloning via profiles
  - Speech-to-text via Whisper-large
  - Voice profile management (create, delete, list, upload samples)
  - 10 languages: en, zh, ja, ko, de, fr, ru, pt, es, it

Telegram integration:
  - Voice messages -> auto-transcribe via Voicebox
  - Text -> TTS via Voicebox -> send back as voice
  - /voice command for profile management
"""

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("skill.audio")

# ============================================================================
# CONFIGURATION
# ============================================================================

VOICEBOX_URL = os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17493")
VOICEBOX_TIMEOUT = int(os.environ.get("VOICEBOX_TIMEOUT", "120"))
VOICEBOX_PATH = os.environ.get("VOICEBOX_PATH", r"C:\Program Files\Voicebox")

SUPPORTED_LANGUAGES = ("en", "zh", "ja", "ko", "de", "fr", "ru", "pt", "es", "it")

# Voicebox text limit per generation request
TEXT_LIMIT = 5000

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma",
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
}

# Global process handle for on-demand lifecycle
_voicebox_process = None


# ============================================================================
# VOICEBOX API HELPERS
# ============================================================================

def _vb_health() -> bool:
    """Check if Voicebox server is reachable."""
    try:
        r = httpx.get(f"{VOICEBOX_URL}/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _vb_get(endpoint: str, **kwargs) -> httpx.Response:
    """GET request to Voicebox."""
    return httpx.get(f"{VOICEBOX_URL}{endpoint}", timeout=VOICEBOX_TIMEOUT, **kwargs)


def _vb_post(endpoint: str, **kwargs) -> httpx.Response:
    """POST request to Voicebox."""
    return httpx.post(f"{VOICEBOX_URL}{endpoint}", timeout=VOICEBOX_TIMEOUT, **kwargs)


def _vb_delete(endpoint: str) -> httpx.Response:
    """DELETE request to Voicebox."""
    return httpx.delete(f"{VOICEBOX_URL}{endpoint}", timeout=VOICEBOX_TIMEOUT)


# ============================================================================
# ON-DEMAND LIFECYCLE (same pattern as ComfyUI)
# ============================================================================

def _ensure_voicebox_running(timeout: int = 60) -> bool:
    """
    Ensure Voicebox server is running. If not, start it automatically.
    Returns True if Voicebox is reachable after this call.
    """
    global _voicebox_process

    if _vb_health():
        return True

    # Try to start voicebox-server.exe
    vb_dir = Path(VOICEBOX_PATH)
    server_exe = vb_dir / "voicebox-server.exe"

    if not server_exe.exists():
        log.error(f"voicebox-server.exe not found at {server_exe}")
        return False

    # Don't start if we already have a process running
    if _voicebox_process is not None and _voicebox_process.poll() is None:
        log.info("Voicebox process already started, waiting...")
    else:
        log.info(f"Starting Voicebox from {vb_dir}...")
        try:
            _voicebox_process = subprocess.Popen(
                [str(server_exe)],
                cwd=str(vb_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            log.info(f"Voicebox started (PID {_voicebox_process.pid})")
        except Exception as e:
            log.error(f"Failed to start Voicebox: {e}")
            return False

    # Wait for it to become reachable
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _vb_health():
            log.info("Voicebox is now reachable")
            return True
        time.sleep(2)

    log.error(f"Voicebox did not start within {timeout}s")
    return False


def stop_voicebox() -> bool:
    """
    Stop the Voicebox server.
    Tries POST /shutdown first, then terminates the process.
    Returns True if stopped successfully.
    """
    global _voicebox_process

    # Try graceful shutdown via API
    try:
        r = httpx.post(f"{VOICEBOX_URL}/shutdown", timeout=10)
        if r.status_code in (200, 202):
            log.info("Voicebox shutdown via API")
            # Wait for process to exit
            if _voicebox_process is not None:
                try:
                    _voicebox_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _voicebox_process.kill()
                _voicebox_process = None
            return True
    except Exception:
        pass

    # Fall back to process termination
    if _voicebox_process is not None and _voicebox_process.poll() is None:
        log.info("Terminating Voicebox process...")
        _voicebox_process.terminate()
        try:
            _voicebox_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _voicebox_process.kill()
        _voicebox_process = None
        log.info("Voicebox process terminated")
        return True

    log.info("Voicebox is not running (nothing to stop)")
    return False


def is_voicebox_running() -> bool:
    """Check if Voicebox is currently running and reachable."""
    return _vb_health()


# ============================================================================
# SPEECH-TO-TEXT (Voicebox Whisper)
# ============================================================================

def transcribe(
    audio_path: str,
    language: Optional[str] = None,
) -> dict:
    """
    Transcribe audio via Voicebox /transcribe endpoint (whisper-large).

    Args:
        audio_path: Path to audio file
        language: Optional language code for hint

    Returns: {success, text, language, duration, error}
    """
    audio_path = os.path.abspath(audio_path)

    if not os.path.isfile(audio_path):
        return {"success": False, "text": "", "error": f"File not found: {audio_path}"}

    ext = Path(audio_path).suffix.lower()
    if ext not in AUDIO_EXTENSIONS:
        return {"success": False, "text": "", "error": f"Unsupported format: {ext}"}

    if not _ensure_voicebox_running():
        return {"success": False, "text": "", "error": "Voicebox server not reachable (auto-start failed)"}

    start = time.time()

    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f)}
            data = {}
            if language:
                data["language"] = language
            r = _vb_post("/transcribe", files=files, data=data)

        if r.status_code != 200:
            return {"success": False, "text": "", "error": f"Voicebox error {r.status_code}: {r.text[:200]}"}

        result = r.json()
        elapsed = round(time.time() - start, 1)
        text = result.get("text", "")
        detected_lang = result.get("language", "?")

        log.info(f"Transcribed {Path(audio_path).name}: {len(text)} chars, {detected_lang}, {elapsed}s")

        return {
            "success": True,
            "text": text,
            "language": detected_lang,
            "duration": elapsed,
        }

    except Exception as e:
        return {"success": False, "text": "", "error": f"Transcription failed: {e}"}


# ============================================================================
# TEXT-TO-SPEECH (Voicebox TTS with voice cloning)
# ============================================================================

def speak(
    text: str,
    output_path: Optional[str] = None,
    language: str = "en",
    profile_id: Optional[str] = None,
    instruct: str = "",
    seed: Optional[int] = None,
) -> dict:
    """
    Generate speech via Voicebox API.

    Steps:
      1. POST /generate with text, profile_id, language
      2. GET /audio/{generation_id} to retrieve the audio file
      3. Save to output_path

    Args:
        text: Text to speak (max 5000 chars)
        output_path: Save to file. If None, generates a temp file.
        language: Language code (en, de, fr, etc.)
        profile_id: Voice profile ID for voice cloning
        instruct: Style/emotion instruction (max 500 chars)
        seed: Reproducible generation seed

    Returns: {success, path, message, generation_id, error}
    """
    if not text.strip():
        return {"success": False, "path": "", "error": "No text provided"}

    if len(text) > TEXT_LIMIT:
        return {"success": False, "path": "", "error": f"Text exceeds {TEXT_LIMIT} char limit ({len(text)} chars)"}

    if language not in SUPPORTED_LANGUAGES:
        language = "en"

    if not _ensure_voicebox_running():
        return {"success": False, "path": "", "error": "Voicebox server not reachable (auto-start failed)"}

    if not output_path:
        output_path = tempfile.mktemp(suffix=".wav", prefix="vb_tts_")

    try:
        # Step 1: Generate
        payload = {"text": text, "language": language}
        if profile_id:
            payload["profile_id"] = profile_id
        if instruct:
            payload["instruct"] = instruct[:500]
        if seed is not None:
            payload["seed"] = seed

        r = _vb_post("/generate", json=payload)
        if r.status_code != 200:
            return {"success": False, "path": "", "error": f"Generate failed ({r.status_code}): {r.text[:200]}"}

        gen_result = r.json()
        generation_id = gen_result.get("generation_id") or gen_result.get("id")

        if not generation_id:
            # Some Voicebox versions return audio directly
            if r.headers.get("content-type", "").startswith("audio/"):
                with open(output_path, "wb") as f:
                    f.write(r.content)
                log.info(f"TTS (direct): {len(text)} chars -> {output_path}")
                return {
                    "success": True,
                    "path": output_path,
                    "message": f"Generated speech: {len(text)} characters",
                }
            return {"success": False, "path": "", "error": "No generation_id returned"}

        # Step 2: Retrieve audio
        audio_r = _vb_get(f"/audio/{generation_id}")
        if audio_r.status_code != 200:
            return {"success": False, "path": "", "error": f"Audio fetch failed ({audio_r.status_code})"}

        with open(output_path, "wb") as f:
            f.write(audio_r.content)

        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            log.info(f"TTS: {len(text)} chars -> {output_path} (profile={profile_id}, lang={language})")
            return {
                "success": True,
                "path": output_path,
                "message": f"Generated speech: {len(text)} characters",
                "generation_id": generation_id,
            }
        else:
            return {"success": False, "path": "", "error": "TTS produced no output"}

    except Exception as e:
        return {"success": False, "path": "", "error": f"TTS failed: {e}"}


# ============================================================================
# VOICE PROFILE MANAGEMENT
# ============================================================================

def list_profiles() -> dict:
    """GET /profiles - List all voice profiles."""
    try:
        r = _vb_get("/profiles")
        if r.status_code != 200:
            return {"success": False, "profiles": [], "error": f"HTTP {r.status_code}"}
        return {"success": True, "profiles": r.json()}
    except Exception as e:
        return {"success": False, "profiles": [], "error": str(e)}


def create_profile(name: str, language: str = "en") -> dict:
    """POST /profiles - Create a new voice profile."""
    try:
        r = _vb_post("/profiles", json={"name": name, "language": language})
        if r.status_code not in (200, 201):
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"success": True, "profile": r.json()}
    except Exception as e:
        return {"success": False, "error": str(e)}


def delete_profile(profile_id: str) -> dict:
    """DELETE /profiles/{id} - Delete a voice profile."""
    try:
        r = _vb_delete(f"/profiles/{profile_id}")
        if r.status_code not in (200, 204):
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"success": True, "message": f"Profile {profile_id} deleted"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def upload_sample(profile_id: str, audio_path: str) -> dict:
    """POST /profiles/{id}/samples - Upload a voice sample."""
    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f)}
            r = _vb_post(f"/profiles/{profile_id}/samples", files=files)
        if r.status_code not in (200, 201):
            return {"success": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return {"success": True, "message": "Sample uploaded"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def list_samples(profile_id: str) -> dict:
    """GET /profiles/{id}/samples - List samples for a profile."""
    try:
        r = _vb_get(f"/profiles/{profile_id}/samples")
        if r.status_code != 200:
            return {"success": False, "samples": [], "error": f"HTTP {r.status_code}"}
        return {"success": True, "samples": r.json()}
    except Exception as e:
        return {"success": False, "samples": [], "error": str(e)}


def get_voicebox_status() -> dict:
    """Combined health + model status."""
    healthy = _vb_health()
    models = {}
    try:
        r = _vb_get("/models/status")
        if r.status_code == 200:
            models = r.json()
    except Exception:
        pass
    return {"healthy": healthy, "models": models}


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run(
    request: str = "",
    action: str = "stt",
    path: str = "",
    text: str = "",
    language: str = "",
    profile_id: str = "",
    **kwargs,
) -> dict:
    """
    Main entry point for the audio skill.

    Actions:
      - stt: Speech-to-text (transcribe audio via Voicebox Whisper)
      - tts: Text-to-speech (generate speech via Voicebox TTS)
      - profiles: List voice profiles
      - status: Voicebox server status

    Returns: dict with results
    """
    action = action.lower().strip()

    # --- STT ---
    if action == "stt":
        if not path:
            return {"success": False, "message": "No audio file path provided. Use path parameter."}
        result = transcribe(path, language=language or None)
        if result["success"]:
            text_preview = result["text"][:500]
            msg = (
                f"*Transcription*\n\n"
                f"Language: `{result['language']}`\n"
                f"Duration: {result['duration']}s\n\n"
                f"{text_preview}"
            )
            if len(result["text"]) > 500:
                msg += f"\n\n...({len(result['text'])} total characters)"
            result["message"] = msg
        else:
            result["message"] = f"Transcription failed: {result.get('error', '?')}"
        return result

    # --- TTS ---
    if action == "tts":
        tts_text = text or request
        if not tts_text:
            return {"success": False, "message": "No text provided for TTS."}
        result = speak(
            tts_text,
            language=language or "en",
            profile_id=profile_id or None,
        )
        if result["success"]:
            result["message"] = f"Speech generated: `{result['path']}`"
        else:
            result["message"] = f"TTS failed: {result.get('error', '?')}"
        return result

    # --- PROFILES ---
    if action == "profiles":
        result = list_profiles()
        if result["success"]:
            profiles = result["profiles"]
            if not profiles:
                result["message"] = "No voice profiles found."
            else:
                lines = [f"*Voice Profiles ({len(profiles)}):*\n"]
                for p in profiles:
                    pid = p.get("id", "?")
                    name = p.get("name", "?")
                    lang = p.get("language", "?")
                    lines.append(f"  `{pid}` - {name} ({lang})")
                result["message"] = "\n".join(lines)
        else:
            result["message"] = f"Failed: {result.get('error', '?')}"
        return result

    # --- STATUS ---
    if action == "status":
        status = get_voicebox_status()
        healthy = "Online" if status["healthy"] else "Offline"
        result = {"success": status["healthy"]}
        result["message"] = f"*Voicebox Status*\nServer: {healthy}"
        return result

    return {"success": False, "message": f"Unknown action: {action}. Use 'stt', 'tts', 'profiles', or 'status'."}
