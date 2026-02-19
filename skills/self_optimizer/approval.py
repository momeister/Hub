"""
skills/self_optimizer/approval.py -- Download/Internet-Genehmigungen
=====================================================================
Schreibt DOWNLOAD_REQUESTS.md / INTERNET_REQUESTS.md und wartet
auf Telegram-User-Genehmigung bevor fortgefahren wird.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from core.dual_logger import BuildLogger

log = logging.getLogger("ai-hub.optimizer.approval")

DOWNLOAD_REQUESTS_FILE = "DOWNLOAD_REQUESTS.md"
INTERNET_REQUESTS_FILE = "INTERNET_REQUESTS.md"
APPROVAL_SIGNAL_FILE = ".optimizer_approval_signal"


class ApprovalManager:
    def __init__(self, project_dir: str, blog: Optional[BuildLogger]):
        self.project_dir = project_dir
        self.blog = blog

    def check_pending_requests(self) -> list[dict]:
        """Pruefen ob Download/Internet-Anfrage-Dateien existieren."""
        pending = []
        for fname, rtype in [
            (DOWNLOAD_REQUESTS_FILE, "download"),
            (INTERNET_REQUESTS_FILE, "internet"),
        ]:
            fpath = os.path.join(self.project_dir, fname)
            if os.path.exists(fpath):
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
                pending.append({
                    "type": rtype,
                    "file": fname,
                    "content": content,
                })
        return pending

    def wait_for_approval(self, timeout: int = 3600) -> bool:
        """
        Auf Genehmigungs-Signal warten (geschrieben vom Telegram-Callback).
        Signal-Datei enthaelt 'approved' oder 'denied'.
        """
        signal_path = os.path.join(self.project_dir, APPROVAL_SIGNAL_FILE)

        # Altes Signal aufraeumen
        try:
            if os.path.exists(signal_path):
                os.remove(signal_path)
        except OSError:
            pass

        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(signal_path):
                try:
                    with open(signal_path, "r", encoding="utf-8") as f:
                        result = f.read().strip().lower()
                    os.remove(signal_path)
                    approved = result == "approved"
                    if self.blog:
                        self.blog.info(
                            f"Genehmigung: {'erteilt' if approved else 'abgelehnt'}"
                        )
                    self._cleanup_request_files()
                    return approved
                except OSError:
                    return False
            time.sleep(2)

        if self.blog:
            self.blog.warning("Genehmigungs-Timeout")
        return False

    def write_approval_signal(self, approved: bool) -> None:
        """Genehmigungs-Signal schreiben (vom Telegram-Callback aufgerufen)."""
        signal_path = os.path.join(self.project_dir, APPROVAL_SIGNAL_FILE)
        with open(signal_path, "w", encoding="utf-8") as f:
            f.write("approved" if approved else "denied")

    def _cleanup_request_files(self) -> None:
        """Anfrage-Dateien aufraeumen nach Genehmigung."""
        for fname in [DOWNLOAD_REQUESTS_FILE, INTERNET_REQUESTS_FILE]:
            fpath = os.path.join(self.project_dir, fname)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except OSError:
                pass
