FROM python:3.12-slim

# Non-root User (uid 1000, gid 1000)
RUN groupadd -g 1000 sandbox && \
    useradd -u 1000 -g 1000 -m -s /bin/sh sandbox

# Minimale Test-Tools installieren
RUN pip install --no-cache-dir pytest mypy ruff 2>/dev/null || true

# Arbeitsverzeichnis (Code wird read-only unter /app/code gemountet)
WORKDIR /app/code

USER 1000:1000

CMD ["python", "--version"]
