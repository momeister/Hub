"""
skills/desktop/skill.py — Desktop/Ordner Organizer
====================================================
Analysiert Dateien mit LLM und sortiert sie intelligent.
Sicherheit: nur definierte Zielpfade, kein System-Ordner.
"""

import json
import logging
import os
import shutil
from pathlib import Path
from core.utils import info, warn, err, send_telegram, clean_json_output
from core.llm_client import call, BASE_URL_V1

log = logging.getLogger("skill.desktop")

# Sicherheits-Whitelist: nur diese Pfade dürfen organisiert werden
SAFE_BASE_PATHS = {
    str(Path.home() / "Desktop"),
    str(Path.home() / "Downloads"),
    str(Path.home() / "Documents"),
    str(Path.home() / "Pictures"),
    str(Path.home() / "Videos"),
    str(Path.home() / "Music"),
}

# Standard-Kategorien + Erweiterungen
DEFAULT_CATEGORIES = {
    "Bilder":       [".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff", ".heic"],
    "Videos":       [".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v"],
    "Musik":        [".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma"],
    "Dokumente":    [".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md"],
    "Tabellen":     [".xlsx", ".xls", ".csv", ".ods"],
    "Praesentation":[".pptx", ".ppt", ".odp"],
    "Code":         [".py", ".js", ".ts", ".rs", ".go", ".java", ".cpp", ".c", ".cs", ".rb", ".php", ".sh", ".bat"],
    "Archive":      [".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"],
    "Programme":    [".exe", ".msi", ".dmg", ".deb", ".rpm", ".appimage"],
    "Fonts":        [".ttf", ".otf", ".woff", ".woff2"],
    "3D_Modelle":   [".obj", ".fbx", ".stl", ".blend", ".dae"],
    "Ebooks":       [".epub", ".mobi", ".azw", ".azw3"],
    "Sonstiges":    [],
}


def _is_safe_path(path: str) -> bool:
    """Prüft ob der Pfad in einer erlaubten Basis-Directory liegt."""
    abs_path = os.path.abspath(path)
    return any(
        abs_path == safe or abs_path.startswith(safe + os.sep)
        for safe in SAFE_BASE_PATHS
    )


def _get_category(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    for category, extensions in DEFAULT_CATEGORIES.items():
        if ext in extensions:
            return category
    return "Sonstiges"


def _analyze_with_llm(files: list[dict], request: str) -> list[dict]:
    """
    Lässt LLM die Organisierungs-Strategie bestimmen.
    Gibt Liste von Aktionen zurück: [{from, to, reason}]
    """
    file_list = "\n".join(
        f"  - {f['name']} ({f['size_kb']:.1f} KB, Typ: {f['category']})"
        for f in files[:50]  # Max 50 Dateien pro LLM-Call
    )

    prompt = (
        f"Organize these files according to: '{request}'\n\n"
        f"Files:\n{file_list}\n\n"
        f"Create a JSON array of move operations:\n"
        f'[{{"file": "filename.ext", "subfolder": "CategoryName", "reason": "short reason"}}]\n\n'
        f"Rules:\n"
        f"  - subfolder must be a simple name (no path separators)\n"
        f"  - Be logical and consistent\n"
        f"  - Group similar files together\n"
        f"  - Return ONLY the JSON array, no explanations"
    )

    result = call(
        model=os.environ.get("CHAT_MODEL", "qwen3-coder-next"),
        prompt=prompt,
        system="You are a file organization expert. Return only valid JSON.",
        base_url=BASE_URL_V1,
        max_tokens=2048,
        temperature=0.1,
    )

    try:
        actions = json.loads(clean_json_output(result))
        return actions if isinstance(actions, list) else []
    except Exception as e:
        warn(f"LLM-Analyse fehlgeschlagen: {e} → Nutze Standard-Kategorien")
        return []


def run(
    request: str,
    target_dir: str = "",
    dry_run: bool = False,
) -> str:
    """
    Organisiert Dateien in einem Verzeichnis.
    
    Args:
        request:    Beschreibung was/wie organisiert werden soll
        target_dir: Zielverzeichnis (default: Desktop)
        dry_run:    Nur anzeigen, nicht wirklich verschieben
    """
    # Zielverzeichnis bestimmen
    if not target_dir:
        desktop = Path.home() / "Desktop"
        target_dir = str(desktop)

    target_dir = os.path.abspath(target_dir)

    # Sicherheits-Check
    if not _is_safe_path(target_dir):
        return (
            f"❌ Sicherheits-Check fehlgeschlagen!\n"
            f"'{target_dir}' ist kein erlaubtes Verzeichnis.\n"
            f"Erlaubt: Desktop, Downloads, Documents, Pictures, Videos, Music"
        )

    if not os.path.isdir(target_dir):
        return f"❌ Verzeichnis nicht gefunden: {target_dir}"

    # Dateien auflesen (nur direkte Kinder, keine Unterordner)
    files_info = []
    for entry in os.scandir(target_dir):
        if entry.is_file():
            size_kb = entry.stat().st_size / 1024
            files_info.append({
                "name":     entry.name,
                "path":     entry.path,
                "size_kb":  size_kb,
                "category": _get_category(entry.name),
            })

    if not files_info:
        return f"📂 Keine Dateien in '{target_dir}' gefunden."

    info(f"Desktop-Skill: {len(files_info)} Dateien in '{target_dir}'")
    send_telegram(
        f"🗂 Desktop-Organizer gestartet\n"
        f"Verzeichnis: {target_dir}\n"
        f"Dateien: {len(files_info)}\n"
        f"{'[DRY RUN]' if dry_run else ''}"
    )

    # LLM für intelligente Kategorisierung
    llm_actions = _analyze_with_llm(files_info, request)

    # Aktionen bauen: LLM-Vorschläge oder Standard-Kategorien
    moves = []
    file_map = {f["name"]: f for f in files_info}

    if llm_actions:
        for action in llm_actions:
            fname   = action.get("file", "")
            subfolder = action.get("subfolder", "Sonstiges")
            reason  = action.get("reason", "")
            if fname in file_map:
                src  = file_map[fname]["path"]
                dest = os.path.join(target_dir, subfolder, fname)
                moves.append((src, dest, reason))
    else:
        # Fallback: Standard-Kategorien nach Extension
        for f in files_info:
            dest = os.path.join(target_dir, f["category"], f["name"])
            moves.append((f["path"], dest, f"Extension → {f['category']}"))

    # Ausführen
    done, skipped, errors = 0, 0, 0
    report_lines = [f"{'[DRY RUN] ' if dry_run else ''}Organisiere {len(moves)} Dateien:\n"]

    for src, dest, reason in moves:
        if src == dest or not os.path.exists(src):
            skipped += 1
            continue
        report_lines.append(f"  → {os.path.basename(src)}  »  {os.path.relpath(dest, target_dir)}")
        if not dry_run:
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # Duplikat-Handling: wenn Datei bereits existiert
                if os.path.exists(dest):
                    base, ext = os.path.splitext(dest)
                    dest = f"{base}_1{ext}"
                shutil.move(src, dest)
                done += 1
            except Exception as e:
                err(f"Move fehlgeschlagen {src}: {e}")
                errors += 1
        else:
            done += 1

    summary = (
        f"{'[DRY RUN] ' if dry_run else ''}✅ Fertig!\n"
        f"Verschoben: {done} | Übersprungen: {skipped} | Fehler: {errors}"
    )
    report_lines.append(f"\n{summary}")
    full_report = "\n".join(report_lines)

    send_telegram(summary)
    return full_report
