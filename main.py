"""
main.py — AI Hub Einstiegspunkt
=================================
Startet den Telegram-Gateway mit Dispatcher und HeartbeatScheduler.
"""

import asyncio
import os
import signal
import sys
import threading

from core.config import load_env

# .env laden
load_env()

# Prüfe Pflicht-Variablen
if not os.environ.get("TELEGRAM_TOKEN"):
    print("[FATAL] TELEGRAM_TOKEN nicht gesetzt. Bitte .env konfigurieren.")
    print("        Vorlage: .env.example → .env kopieren und ausfüllen.")
    sys.exit(1)


def _start_heartbeat_background():
    """HeartbeatScheduler in einem separaten Thread mit eigenem Event-Loop starten."""
    try:
        from core.heartbeat import get_heartbeat
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        scheduler = get_heartbeat()
        loop.run_until_complete(scheduler.start())
        loop.run_forever()
    except Exception as exc:
        print(f"[WARN] HeartbeatScheduler nicht gestartet: {exc}")


# HeartbeatScheduler als Daemon-Thread starten
heartbeat_thread = threading.Thread(
    target=_start_heartbeat_background,
    daemon=True,
    name="heartbeat",
)
heartbeat_thread.start()

# Starte Gateway
from core.telegram_gateway import main
main()

# Cleanup beim Beenden
try:
    from core.heartbeat import get_heartbeat
    hb = get_heartbeat()
    asyncio.run(hb.stop())
except Exception:
    pass
