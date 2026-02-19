"""
core/config.py — Central configuration and env loading
=======================================================
Keeps all environment defaults and paths in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
SKILLS_DIR = ROOT_DIR / "skills"


def load_env(path: Path = ENV_PATH) -> None:
    """Load .env into os.environ without overwriting existing values."""
    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())
    except OSError:
        # Fail silently: env is optional
        pass


def is_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


@dataclass(frozen=True)
class TelegramConfig:
    token: str
    chat_id_raw: str
    allowed_ids: set[int]


@dataclass(frozen=True)
class ModelConfig:
    dispatcher_model: str
    chat_model: str


@dataclass(frozen=True)
class PathsConfig:
    root_dir: Path
    skills_dir: Path
    output_dir: Path


@dataclass(frozen=True)
class BuilderConfig:
    image: str


@dataclass(frozen=True)
class ServiceConfig:
    ollama_host: str
    ollama_base_url: str
    ollama_v1_url: str
    voicebox_url: str
    comfyui_url: str

    def port_for(self, url: str, fallback: int) -> int:
        try:
            parsed = urlparse(url)
            return parsed.port or fallback
        except Exception:
            return fallback


def get_telegram_config() -> TelegramConfig:
    token = os.environ.get("TELEGRAM_TOKEN", "")
    chat_id_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    allowed_ids = {int(x) for x in chat_id_raw.split(",") if x.strip().isdigit()}
    return TelegramConfig(token=token, chat_id_raw=chat_id_raw, allowed_ids=allowed_ids)


def get_model_config() -> ModelConfig:
    return ModelConfig(
        dispatcher_model=os.environ.get("DISPATCHER_MODEL", "glm-4-flash"),
        chat_model=os.environ.get("CHAT_MODEL", "qwen2.5:7b"),
    )


def get_paths_config() -> PathsConfig:
    output_dir = Path(os.environ.get("OUTPUT_DIR", str(ROOT_DIR / "output")))
    return PathsConfig(root_dir=ROOT_DIR, skills_dir=SKILLS_DIR, output_dir=output_dir)


def get_builder_config() -> BuilderConfig:
    return BuilderConfig(image=os.environ.get("BUILDER_IMAGE", "ai-cluster"))


def get_service_config() -> ServiceConfig:
    host = "host.docker.internal" if is_in_docker() else "localhost"
    base = f"http://{host}:11434"
    return ServiceConfig(
        ollama_host=host,
        ollama_base_url=base,
        ollama_v1_url=f"{base}/v1",
        voicebox_url=os.environ.get("VOICEBOX_URL", "http://127.0.0.1:17493"),
        comfyui_url=os.environ.get("COMFYUI_URL", "http://localhost:8188"),
    )
