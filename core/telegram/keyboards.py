"""
core/telegram/keyboards.py — Inline keyboards
=============================================
Telegram inline keyboard builders used across features.
"""

from __future__ import annotations

from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.services_status import get_services_status
from core.telegram.settings import OUTPUT_BASE, SERVICES_CFG


def kb_services() -> InlineKeyboardMarkup:
    services = get_services_status(SERVICES_CFG)
    rows = []
    for svc in services:
        status = "Online" if svc["online"] else "Offline"
        label = f"{svc['name']}: {status} ({svc['port']})"
        if svc["can_stop"]:
            svc_key = svc["name"].lower()
            if svc["online"]:
                rows.append([
                    InlineKeyboardButton(label, callback_data="svc_noop"),
                    InlineKeyboardButton("Stop", callback_data=f"svc_stop_{svc_key}"),
                ])
            else:
                rows.append([
                    InlineKeyboardButton(label, callback_data="svc_noop"),
                    InlineKeyboardButton("Start", callback_data=f"svc_start_{svc_key}"),
                ])
        else:
            rows.append([InlineKeyboardButton(label, callback_data="svc_noop")])

    rows.append([InlineKeyboardButton("Refresh", callback_data="svc_refresh")])
    return InlineKeyboardMarkup(rows)


def kb_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("FAST       (r1:8b / qwen:7b)",      callback_data="mode_1")],
        [InlineKeyboardButton("AVERAGE    (r1:32b / qwen:14b)",    callback_data="mode_2")],
        [InlineKeyboardButton("GOD MODE  (gpt-oss / qwen3)",       callback_data="mode_3")],
        [InlineKeyboardButton("UNCENSORED (qwen3-abliterated x2)", callback_data="mode_4")],
        [InlineKeyboardButton("Custom...",                          callback_data="mode_5")],
    ])


def kb_scope() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Automatisch",                    callback_data="scope_1")],
        [InlineKeyboardButton("Kompakt  (max ~10 Dateien)",     callback_data="scope_2")],
        [InlineKeyboardButton("Voll     (Clean Arch + Layer)", callback_data="scope_3")],
    ])


def kb_tests() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Keine Tests",     callback_data="tests_1")],
        [InlineKeyboardButton("Mit Unit-Tests",  callback_data="tests_2")],
    ])


def kb_internet() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Kein Internet (schneller)", callback_data="internet_n")],
        [InlineKeyboardButton("Internet nutzen (bei Bedarf)", callback_data="internet_y")],
    ])


def kb_yesno(yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Ja, starten", callback_data=yes),
        InlineKeyboardButton("Abbrechen",   callback_data=no),
    ]])


def kb_opmode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Neues Projekt",                   callback_data="opmode_1")],
        [InlineKeyboardButton("Bestehendes Projekt bearbeiten", callback_data="opmode_2")],
    ])


def kb_project_list() -> InlineKeyboardMarkup:
    from skills.builder.builder_core import list_projects

    output_base = str(Path(OUTPUT_BASE).resolve())
    projects = list_projects(output_base)
    if not projects:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Keine Projekte vorhanden", callback_data="editproject_none")],
        ])
    buttons = []
    for p in projects[:8]:
        label = f"{p['name']}  ({p['language']}, {p['files_count']} files)"
        buttons.append([InlineKeyboardButton(label, callback_data=f"editproject_{p['name']}")])
    buttons.append([InlineKeyboardButton("Abbrechen", callback_data="confirm_no")])
    return InlineKeyboardMarkup(buttons)


def kb_image_model() -> InlineKeyboardMarkup:
    try:
        from skills.comfyui.skill import get_available_models
        models = get_available_models()
    except Exception:
        models = []
    if not models:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("No workflows found", callback_data="imgmodel_none")],
        ])
    buttons = [[InlineKeyboardButton(m, callback_data=f"imgmodel_{m}")] for m in models]
    return InlineKeyboardMarkup(buttons)


def kb_image_size() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1248x821 (Landscape)",  callback_data="imgsize_1248_821")],
        [InlineKeyboardButton("821x1248 (Portrait)",   callback_data="imgsize_821_1248")],
        [InlineKeyboardButton("1024x1024 (Square)",    callback_data="imgsize_1024_1024")],
        [InlineKeyboardButton("1280x720 (Wide)",       callback_data="imgsize_1280_720")],
        [InlineKeyboardButton("512x512 (Small)",       callback_data="imgsize_512_512")],
    ])


def kb_image_amount() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1",  callback_data="imgamount_1"),
         InlineKeyboardButton("2",  callback_data="imgamount_2"),
         InlineKeyboardButton("4",  callback_data="imgamount_4")],
    ])


def kb_image_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Generate", callback_data="imgconfirm_yes"),
        InlineKeyboardButton("Cancel",   callback_data="imgconfirm_no"),
    ]])


def kb_voice_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Generate Voice",        callback_data="voice_generate")],
        [InlineKeyboardButton("List Profiles",         callback_data="voice_list")],
        [InlineKeyboardButton("Create Profile",        callback_data="voice_create")],
        [InlineKeyboardButton("Select Profile (TTS)",  callback_data="voice_select")],
        [InlineKeyboardButton("Delete Profile",        callback_data="voice_delete")],
        [InlineKeyboardButton("Set Language",          callback_data="voice_lang")],
        [InlineKeyboardButton("Voicebox Status",       callback_data="voice_status")],
    ])


def kb_voice_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English",    callback_data="vlang_en"),
         InlineKeyboardButton("Deutsch",    callback_data="vlang_de")],
        [InlineKeyboardButton("Francais",   callback_data="vlang_fr"),
         InlineKeyboardButton("Espanol",    callback_data="vlang_es")],
        [InlineKeyboardButton("Italiano",   callback_data="vlang_it"),
         InlineKeyboardButton("Portugues",  callback_data="vlang_pt")],
        [InlineKeyboardButton("Russian",    callback_data="vlang_ru"),
         InlineKeyboardButton("Chinese",    callback_data="vlang_zh")],
        [InlineKeyboardButton("Japanese",   callback_data="vlang_ja"),
         InlineKeyboardButton("Korean",     callback_data="vlang_ko")],
    ])


def kb_voice_profiles_select(profiles: list) -> InlineKeyboardMarkup:
    if not profiles:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("No profiles found", callback_data="vprofile_none")],
        ])
    buttons = []
    for p in profiles[:10]:
        pid = p.get("id", "?")
        name = p.get("name", "?")
        lang = p.get("language", "?")
        label = f"{name} ({lang})"
        buttons.append([InlineKeyboardButton(label, callback_data=f"vprofile_{pid}")])
    buttons.append([InlineKeyboardButton("Cancel", callback_data="voice_cancel")])
    return InlineKeyboardMarkup(buttons)


def kb_voice_profile_lang() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English",    callback_data="vplang_en"),
         InlineKeyboardButton("Deutsch",    callback_data="vplang_de")],
        [InlineKeyboardButton("Francais",   callback_data="vplang_fr"),
         InlineKeyboardButton("Espanol",    callback_data="vplang_es")],
        [InlineKeyboardButton("Italiano",   callback_data="vplang_it"),
         InlineKeyboardButton("Portugues",  callback_data="vplang_pt")],
        [InlineKeyboardButton("Russian",    callback_data="vplang_ru"),
         InlineKeyboardButton("Chinese",    callback_data="vplang_zh")],
        [InlineKeyboardButton("Japanese",   callback_data="vplang_ja"),
         InlineKeyboardButton("Korean",     callback_data="vplang_ko")],
    ])


def kb_voice_delete_confirm(profile_id: str, profile_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"Delete '{profile_name}'", callback_data=f"vdelete_yes_{profile_id}"),
        InlineKeyboardButton("Cancel", callback_data="voice_cancel"),
    ]])


# ── Self-Optimizer Keyboards ──────────────────────────────────────────────

def kb_optimizer_mode() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Task (bestimmtes Feature/Fix)",   callback_data="optmode_task")],
        [InlineKeyboardButton("Auto (KI entscheidet)",           callback_data="optmode_auto")],
    ])


def kb_optimizer_iterations() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("1x",  callback_data="optiter_1"),
         InlineKeyboardButton("3x",  callback_data="optiter_3")],
        [InlineKeyboardButton("10x", callback_data="optiter_10"),
         InlineKeyboardButton("Endlos (bis Stop)", callback_data="optiter_0")],
    ])


def kb_optimizer_model() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("FAST       (r1:8b / qwen:7b)",  callback_data="optmodel_1")],
        [InlineKeyboardButton("AVERAGE    (r1:32b / qwen:14b)", callback_data="optmodel_2")],
        [InlineKeyboardButton("GOD MODE  (gpt-oss / qwen3)",    callback_data="optmodel_3")],
    ])


def kb_optimizer_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Starten", callback_data="optconfirm_yes"),
        InlineKeyboardButton("Abbrechen", callback_data="optconfirm_no"),
    ]])


def kb_optimizer_merge() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Merge genehmigen", callback_data="optmerge_yes"),
        InlineKeyboardButton("Ablehnen", callback_data="optmerge_no"),
    ]])


def kb_optimizer_approval() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Genehmigen", callback_data="optapproval_yes"),
        InlineKeyboardButton("Ablehnen",   callback_data="optapproval_no"),
    ]])


def kb_optimizer_running() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Pause",  callback_data="optctl_pause"),
         InlineKeyboardButton("Stop",   callback_data="optctl_stop")],
        [InlineKeyboardButton("Status", callback_data="optctl_status")],
    ])
