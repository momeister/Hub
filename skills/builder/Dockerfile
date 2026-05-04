FROM python:3.12-slim

WORKDIR /app

# System-Tools (nodejs/npm for JS/TS project verification)
# NOTE: Rust (cargo) and Go are NOT installed — too large for container.
# Rust/Go projects are verified on the host machine only.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl mypy nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Ruff (standalone binary)
RUN curl -fsSL https://astral.sh/ruff/install.sh | sh && \
    cp /root/.local/bin/ruff /usr/local/bin/ruff || \
    cp ~/.local/bin/ruff /usr/local/bin/ruff || true

# Python Dependencies
# Build-Kontext = Hub-Root (docker build -f skills/builder/Dockerfile .)
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Core-Module (utils, llm_client, etc.)
COPY core/ ./core/

# Builder Code (package + engine modules)
COPY skills/__init__.py ./skills/__init__.py
COPY skills/builder/ ./skills/builder/

# Output directory (shared with host for approval signals and project files)
VOLUME ["/app/output"]

# Force JSON-line output mode for Docker builds
ENV TRIGGERED_BY=telegram

# Uncomment to skip UX polish phase for faster builds:
# ENV BUILDER_SKIP_POLISH=1

ENTRYPOINT ["python", "-u", "-m", "skills.builder.builder_core"]
