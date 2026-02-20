"""
core/memory.py — Persistentes Memory-System
=============================================
Verwaltet eine MEMORY.md Datei im Root-Verzeichnis als
human-readable Wissensspeicher fuer den AI Hub.

Sektionen: ## Facts, ## Sessions, ## Projects
Auto-Archivierung bei >50KB in MEMORY_archive_YYYY-MM.md
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, date
from typing import Optional

from core.config import ROOT_DIR

log = logging.getLogger("ai-hub.memory")

MEMORY_FILE = ROOT_DIR / "MEMORY.md"
MAX_MEMORY_SIZE = 50 * 1024  # 50KB
MAX_CONTEXT_ENTRIES = 30


class MemoryManager:
    """
    Persistenter Speicher fuer Fakten, Sessions und Projekte.
    Schreibt/liest eine Markdown-Datei die auch manuell lesbar ist.
    """

    def __init__(self, memory_path: str = ""):
        self.path = memory_path or str(MEMORY_FILE)
        self._facts: list[dict] = []
        self._sessions: list[dict] = []
        self._projects: list[dict] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # Laden / Speichern
    # ------------------------------------------------------------------

    def load(self) -> None:
        """MEMORY.md einlesen und in interne Strukturen parsen."""
        if not os.path.exists(self.path):
            self._ensure_skeleton()
            self._loaded = True
            log.info("MEMORY.md neu erstellt")
            return

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            log.warning(f"MEMORY.md konnte nicht gelesen werden: {exc}")
            self._ensure_skeleton()
            self._loaded = True
            return

        self._parse(content)
        self._loaded = True
        log.info(
            f"MEMORY.md geladen: {len(self._facts)} Facts, "
            f"{len(self._sessions)} Sessions, {len(self._projects)} Projects"
        )

    def save(self) -> None:
        """Interne Strukturen als Markdown zurueckschreiben."""
        self._check_archive()
        content = self._render()
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as exc:
            log.warning(f"MEMORY.md konnte nicht geschrieben werden: {exc}")

    # ------------------------------------------------------------------
    # Oeffentliche API
    # ------------------------------------------------------------------

    def remember(self, key: str, value: str, category: str = "general") -> None:
        """Eine Tatsache speichern.  Doppelte Keys werden aktualisiert."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for fact in self._facts:
            if fact["key"] == key and fact["category"] == category:
                fact["value"] = value
                fact["date"] = now
                self.save()
                return
        self._facts.append({
            "key": key,
            "value": value,
            "category": category,
            "date": now,
        })
        self.save()

    def recall(self, query: str) -> str:
        """Relevante Erinnerungen zu einer Anfrage zurueckgeben."""
        query_lower = query.lower()
        tokens = set(query_lower.split())

        scored: list[tuple[float, str]] = []

        for fact in self._facts:
            text = f"{fact['key']} {fact['value']} {fact['category']}"
            score = sum(1 for t in tokens if t in text.lower())
            if score > 0:
                scored.append((
                    score,
                    f"[{fact['category']}] {fact['key']}: {fact['value']} ({fact['date']})"
                ))

        for proj in self._projects:
            text = f"{proj['name']} {proj.get('summary', '')} {proj.get('language', '')}"
            score = sum(1 for t in tokens if t in text.lower())
            if score > 0:
                scored.append((
                    score,
                    f"[project] {proj['name']} ({proj.get('language', '?')}, "
                    f"{proj.get('date', '?')}): {proj.get('summary', '')}"
                ))

        for sess in self._sessions[-10:]:
            text = sess.get("summary", "")
            score = sum(1 for t in tokens if t in text.lower())
            if score > 0:
                scored.append((
                    score,
                    f"[session {sess.get('date', '?')}] {text[:200]}"
                ))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return ""
        return "\n".join(entry for _, entry in scored[:10])

    def summarize_session(self, messages: list[dict]) -> None:
        """Session-Zusammenfassung speichern (die letzten N Nachrichten)."""
        if not messages:
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        user_msgs = [
            m["content"][:200] for m in messages
            if m.get("role") == "user" and m.get("content")
        ]
        if not user_msgs:
            return

        topics = ", ".join(user_msgs[:5])
        summary = f"Themen: {topics}"
        if len(summary) > 500:
            summary = summary[:497] + "..."

        self._sessions.append({
            "date": now,
            "summary": summary,
            "message_count": len(messages),
        })

        # Nur letzte 50 Sessions behalten
        if len(self._sessions) > 50:
            self._sessions = self._sessions[-50:]

        self.save()

    def add_project(
        self,
        name: str,
        language: str,
        summary: str,
        framework: str = "",
    ) -> None:
        """Fertig gebautes Projekt in Memory aufnehmen."""
        now = datetime.now().strftime("%Y-%m-%d")
        # Existierendes Projekt aktualisieren
        for proj in self._projects:
            if proj["name"] == name:
                proj.update({
                    "date": now,
                    "language": language,
                    "framework": framework,
                    "summary": summary,
                })
                self.save()
                log.info(f"Projekt aktualisiert in Memory: {name}")
                return
        self._projects.append({
            "name": name,
            "date": now,
            "language": language,
            "framework": framework,
            "summary": summary,
        })
        self.save()
        log.info(f"Projekt gespeichert in Memory: {name}")

    def add_optimization(
        self,
        date_str: str,
        file_changed: str,
        description: str,
        tests_passed: bool,
    ) -> None:
        """Optimierungs-Ergebnis in Facts speichern."""
        self.remember(
            key=f"optimization_{date_str}_{file_changed}",
            value=f"{description} | Tests: {'OK' if tests_passed else 'FAIL'}",
            category="optimization",
        )

    def get_optimization_history(self) -> list[dict]:
        """Alle bisherigen Optimierungen aus Memory lesen."""
        history = []
        for fact in self._facts:
            if fact.get("category") == "optimization":
                parts = fact["value"].rsplit(" | Tests: ", 1)
                desc = parts[0] if parts else fact["value"]
                tests_ok = parts[1] == "OK" if len(parts) > 1 else False
                # Key-Format: optimization_YYYY-MM-DD HH:MM_filename
                key_parts = fact["key"].split("_", 2)
                history.append({
                    "date": fact.get("date", ""),
                    "file": key_parts[2] if len(key_parts) > 2 else "?",
                    "description": desc,
                    "tests_passed": tests_ok,
                })
        return history

    def get_context_for_llm(self) -> str:
        """
        Kompakter String fuer System-Prompts.
        Enthaelt die letzten N Eintraege aus allen Sektionen.
        """
        lines = []

        if self._facts:
            recent_facts = self._facts[-MAX_CONTEXT_ENTRIES:]
            lines.append("Known facts:")
            for f in recent_facts:
                lines.append(f"  - [{f['category']}] {f['key']}: {f['value']}")

        if self._projects:
            lines.append("Built projects:")
            for p in self._projects[-10:]:
                fw = f" ({p['framework']})" if p.get("framework") else ""
                lines.append(
                    f"  - {p['name']} [{p['language']}{fw}]: {p.get('summary', '')[:100]}"
                )

        if self._sessions:
            lines.append("Recent sessions:")
            for s in self._sessions[-5:]:
                lines.append(f"  - {s['date']}: {s['summary'][:150]}")

        result = "\n".join(lines)
        # Maximal 3000 Zeichen fuer den Kontext
        if len(result) > 3000:
            result = result[:2997] + "..."
        return result

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse(self, content: str) -> None:
        """Markdown-Inhalt in Strukturen parsen."""
        self._facts = []
        self._sessions = []
        self._projects = []

        current_section = ""
        current_entry_lines: list[str] = []

        for line in content.split("\n"):
            stripped = line.strip()

            # Sektionsheader erkennen
            if stripped.startswith("## "):
                # Vorherigen Eintrag abschliessen
                self._flush_entry(current_section, current_entry_lines)
                current_entry_lines = []
                current_section = stripped[3:].strip().lower()
                continue

            if stripped.startswith("### "):
                # Sub-Eintrag abschliessen und neuen beginnen
                self._flush_entry(current_section, current_entry_lines)
                current_entry_lines = [stripped]
                continue

            if stripped.startswith("- ") and current_section in ("facts",):
                self._flush_entry(current_section, current_entry_lines)
                current_entry_lines = [stripped]
                continue

            if stripped:
                current_entry_lines.append(stripped)

        self._flush_entry(current_section, current_entry_lines)

    def _flush_entry(self, section: str, lines: list[str]) -> None:
        """Gesammelten Eintrag in die richtige Struktur einsortieren."""
        if not lines:
            return

        text = " ".join(lines)

        if section == "facts":
            # Format: - [category] key: value (date)
            m = re.match(
                r"-\s*\[([^\]]+)\]\s*([^:]+):\s*(.+?)(?:\s*\((\d{4}-\d{2}-\d{2}[^)]*)\))?\s*$",
                text,
            )
            if m:
                self._facts.append({
                    "category": m.group(1).strip(),
                    "key": m.group(2).strip(),
                    "value": m.group(3).strip(),
                    "date": m.group(4) or "",
                })
            else:
                # Einfacher Key: Value ohne Kategorie
                m2 = re.match(r"-\s*([^:]+):\s*(.+)", text)
                if m2:
                    self._facts.append({
                        "category": "general",
                        "key": m2.group(1).strip(),
                        "value": m2.group(2).strip(),
                        "date": "",
                    })

        elif section == "sessions":
            # Format: ### YYYY-MM-DD HH:MM \n summary text
            m = re.match(r"###\s*(\d{4}-\d{2}-\d{2}\s*\d{2}:\d{2})\s*(.*)", text)
            if m:
                self._sessions.append({
                    "date": m.group(1).strip(),
                    "summary": m.group(2).strip(),
                    "message_count": 0,
                })
            elif text.startswith("### "):
                self._sessions.append({
                    "date": text[4:20].strip(),
                    "summary": text[20:].strip(),
                    "message_count": 0,
                })

        elif section == "projects":
            # Format: ### ProjectName \n details...
            m = re.match(r"###\s*(.+)", text)
            if m:
                rest = m.group(1).strip()
                proj = {"name": rest, "date": "", "language": "", "framework": "", "summary": ""}
                # Felder extrahieren
                for part in text.split("|"):
                    part = part.strip()
                    if part.startswith("Lang:"):
                        proj["language"] = part[5:].strip()
                    elif part.startswith("Framework:"):
                        proj["framework"] = part[10:].strip()
                    elif part.startswith("Date:"):
                        proj["date"] = part[5:].strip()
                    elif part.startswith("Summary:"):
                        proj["summary"] = part[8:].strip()
                if not proj["name"].startswith("###"):
                    self._projects.append(proj)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render(self) -> str:
        """Interne Strukturen als Markdown rendern."""
        lines = ["# AI Hub Memory", "", ""]

        # Facts
        lines.append("## Facts")
        lines.append("")
        for f in self._facts:
            date_part = f" ({f['date']})" if f.get("date") else ""
            lines.append(f"- [{f['category']}] {f['key']}: {f['value']}{date_part}")
        lines.append("")

        # Sessions
        lines.append("## Sessions")
        lines.append("")
        for s in self._sessions:
            lines.append(f"### {s['date']}")
            lines.append(f"{s['summary']}")
            if s.get("message_count"):
                lines.append(f"Messages: {s['message_count']}")
            lines.append("")

        # Projects
        lines.append("## Projects")
        lines.append("")
        for p in self._projects:
            fw_part = f" | Framework: {p['framework']}" if p.get("framework") else ""
            lines.append(
                f"### {p['name']}\n"
                f"Date: {p.get('date', '?')} | Lang: {p.get('language', '?')}"
                f"{fw_part}\n"
                f"Summary: {p.get('summary', '')}\n"
            )

        return "\n".join(lines)

    def _ensure_skeleton(self) -> None:
        """Leere MEMORY.md Struktur erstellen."""
        self._facts = []
        self._sessions = []
        self._projects = []
        self.save()

    # ------------------------------------------------------------------
    # Auto-Archivierung
    # ------------------------------------------------------------------

    def _check_archive(self) -> None:
        """Bei Ueberschreitung von MAX_MEMORY_SIZE archivieren."""
        if not os.path.exists(self.path):
            return

        try:
            size = os.path.getsize(self.path)
        except OSError:
            return

        if size <= MAX_MEMORY_SIZE:
            return

        # Archiv-Datei erstellen
        now = datetime.now()
        archive_name = f"MEMORY_archive_{now.strftime('%Y-%m')}.md"
        archive_path = os.path.join(os.path.dirname(self.path), archive_name)

        log.info(
            f"MEMORY.md ist {size / 1024:.1f}KB (max {MAX_MEMORY_SIZE / 1024:.0f}KB) "
            f"→ Archiviere in {archive_name}"
        )

        try:
            # Bestehende Datei an Archiv anhaengen
            existing_archive = ""
            if os.path.exists(archive_path):
                with open(archive_path, "r", encoding="utf-8") as f:
                    existing_archive = f.read()

            with open(self.path, "r", encoding="utf-8") as f:
                current = f.read()

            with open(archive_path, "w", encoding="utf-8") as f:
                if existing_archive:
                    f.write(existing_archive)
                    f.write("\n\n---\n\n")
                f.write(f"# Archive {now.strftime('%Y-%m-%d %H:%M')}\n\n")
                f.write(current)

            # Alte Eintraege kuerzen: nur letzte 20 Facts, 10 Sessions, alle Projects
            self._facts = self._facts[-20:]
            self._sessions = self._sessions[-10:]
            # Projects bleiben alle

            log.info(f"Archiviert nach {archive_name}, Memory gekuerzt")

        except OSError as exc:
            log.warning(f"Archivierung fehlgeschlagen: {exc}")


# ---------------------------------------------------------------------------
# Singleton-Instanz
# ---------------------------------------------------------------------------

_instance: Optional[MemoryManager] = None


def get_memory() -> MemoryManager:
    """Globale MemoryManager-Instanz zurueckgeben (Singleton)."""
    global _instance
    if _instance is None:
        _instance = MemoryManager()
        _instance.load()
    return _instance
