"""
core/telegram/settings.py — Telegram gateway settings
=====================================================
Centralizes env loading and settings used by gateway modules.
"""

from __future__ import annotations

from core.config import (
    load_env,
    get_builder_config,
    get_model_config,
    get_paths_config,
    get_service_config,
    get_telegram_config,
)

load_env()

_telegram_cfg = get_telegram_config()
_model_cfg = get_model_config()
_paths_cfg = get_paths_config()
_builder_cfg = get_builder_config()
_services_cfg = get_service_config()

TELEGRAM_TOKEN = _telegram_cfg.token
TELEGRAM_CHAT_ID = _telegram_cfg.chat_id_raw
ALLOWED_IDS = _telegram_cfg.allowed_ids

BUILDER_IMAGE = _builder_cfg.image
OUTPUT_BASE = str(_paths_cfg.output_dir)

DISPATCHER_MODEL = _model_cfg.dispatcher_model
CHAT_MODEL = _model_cfg.chat_model

SERVICES_CFG = _services_cfg
VOICEBOX_URL = _services_cfg.voicebox_url


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_IDS:
        return True
    return chat_id in ALLOWED_IDS
