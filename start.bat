@echo off
cd /d "%~dp0"

echo.
echo  AI Hub - Starting...
echo.

REM Pruefe ob .env vorhanden
if not exist ".env" (
    echo [FEHLER] .env nicht gefunden!
    echo Kopiere .env.example nach .env und konfiguriere deine Werte.
    pause
    exit /b 1
)

REM Pruefe ob Python vorhanden
python --version >nul 2>&1
if errorlevel 1 (
    echo [FEHLER] Python nicht gefunden!
    pause
    exit /b 1
)

REM Pruefe ob Requirements installiert
python -c "import telegram" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installiere Requirements...
    pip install -r requirements.txt
)

REM Pruefe ob Builder-Image vorhanden
docker image inspect ai-cluster >nul 2>&1
if errorlevel 1 (
    echo [INFO] Builder-Image nicht gefunden - baue...
    docker build -f skills\builder\Dockerfile -t ai-cluster .
)

echo [OK] Starte AI Hub...
python main.py

pause
