"""
main.py — AI Hub Einstiegspunkt
=================================
Startet den Telegram-Gateway mit Dispatcher.
"""

import os
import sys

from core.config import load_env

# .env laden
load_env()

# Prüfe Pflicht-Variablen
if not os.environ.get("TELEGRAM_TOKEN"):
    print("[FATAL] TELEGRAM_TOKEN nicht gesetzt. Bitte .env konfigurieren.")
    print("        Vorlage: .env.example → .env kopieren und ausfüllen.")
    sys.exit(1)

# Starte Gateway
from core.telegram_gateway import main
main()
