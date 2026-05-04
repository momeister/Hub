# AI Hub - Telegram AI Development Assistant

AI Hub is a modular AI-powered development system controlled via Telegram.
Send an instruction via Telegram, and the Builder creates a complete project using
local LLMs (Ollama) with a 5-agent pipeline architecture.

## Hardware Requirements

Tested for:
- **GPU**: NVIDIA RTX 5070 Ti (12GB VRAM) or similar
- **RAM**: 64GB system memory
- **OS**: Windows 10/11
- **Storage**: SSD recommended for model loading speed

Models are loaded **sequentially** (one at a time) to fit in VRAM.
With 64GB RAM, the system can handle 128k context windows via `qwen3-coder-next`.

## Quick Start

### 1. Prerequisites

- [Python 3.11+](https://www.python.org/downloads/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for isolated builds)
- [Ollama](https://ollama.ai/) running locally on port 11434
- A Telegram Bot token (from [@BotFather](https://t.me/BotFather))

### 2. Pull Required Models

```bash
ollama pull glm4:9b
ollama pull qwen3-coder-next
ollama pull deepseek-r1:8b
```

For GOD MODE (recommended with your hardware):
```bash
ollama pull gpt-oss:120b
```

### 3. Configure

```bash
copy .env.example .env
```

Edit `.env` and fill in:
- `TELEGRAM_TOKEN` - Your bot token from BotFather
- `TELEGRAM_CHAT_ID` - Your Telegram user ID (comma-separated for multiple users)
- `DOWNLOAD_DIR` - Your downloads folder path

### 4. Run

**Option A: Quick start (Windows)**
```bash
start.bat
```

**Option B: Manual start**
```bash
pip install -r requirements.txt
docker build -f skills\builder\Dockerfile -t ai-cluster .
python main.py
```

The bot will start listening for Telegram messages.

## Builder Agent Pipeline

The builder uses **5 sequential agents** that run one at a time:

```
User Instruction (Telegram)
        |
        v
+------------------+
| 1. PLANNER       |  Manager model (deepseek-r1 / gpt-oss)
|  - Analyzes goal  |  - Picks language, framework, tech stack
|  - Designs arch   |  - Creates file structure + dependency order
|  - Self-reviews   |  - Reviews own blueprint for issues
+------------------+
        |
        v
+------------------+
| 2. RETRIEVER     |  No LLM needed (CPU only)
|  - Validates plan |  - Ensures critical files exist
|  - Safe stack chk |  - Validates dependencies are real packages
|  - Sets up venv   |  - Creates Python virtual environment
+------------------+
        |
        v
+------------------+
| 3. CODER         |  Coder model (qwen3-coder-next)
|  - Gen skeletons  |  - All file signatures in one call
|  - Fill-in loop   |  - Implements each file with full context
|  - Inline repair  |  - Fixes compile errors per-file
+------------------+
        |
        v
+------------------+
| 4. EXECUTOR      |  No LLM needed (CPU only)
|  - Compile check  |  - Runs language-specific compiler
|  - Install deps   |  - pip/npm/cargo in isolated environment
|  - Sandbox test   |  - Runs the project, checks if it starts
|  - Port detection  |  - Detects web servers automatically
+------------------+
        |
        v
+------------------+
| 5. CRITIC        |  Both models
|  - Diagnose fails |  - Analyzes runtime errors
|  - Self-correct   |  - Fixes and re-tests iteratively
|  - UX polish      |  - Suggests user experience improvements
|  - Apply polish   |  - Applies top improvements
+------------------+
        |
        v
  Project Complete
  (README + start.bat generated)
```

### Why Sequential Agents?

With 12GB VRAM, only one large model fits at a time. The pipeline is designed so:
- **Planner** loads the manager model (reasoning-heavy)
- **Retriever** runs CPU-only (manager model can be unloaded)
- **Coder** loads the coder model (code generation)
- **Executor** runs CPU-only (coder model stays for potential critic repairs)
- **Critic** reuses the coder model for repairs, then manager for polish suggestions

## Build Modes

| Mode | Manager | Coder | Context | Best For |
|------|---------|-------|---------|----------|
| FAST | deepseek-r1:8b | qwen2.5-coder:7b | 64k | Quick prototypes |
| AVERAGE | deepseek-r1:32b | qwen2.5-coder:14b | 64k | Balanced quality/speed |
| GOD MODE | gpt-oss:120b | qwen3-coder-next | 128k | Maximum quality |
| UNCENSORED | qwen3-coder-next-abliterated | same | 128k | No content filters |
| CUSTOM | your choice | your choice | custom | Full control |

## Context Window

With RTX 5070 Ti + 64GB RAM, `qwen3-coder-next` supports **131,072 tokens** (128k).
This is set as the default for GOD MODE and UNCENSORED modes.

The context window is used efficiently:
- Skeleton phase: all file signatures in one call
- Fill-in phase: full project context per file (skeletons + already-written code)
- Dynamic truncation at 80% capacity to prevent OOM

## Virtual Environments

Generated Python projects automatically use virtual environments:
- `.venv` is created in the project directory
- Dependencies are installed via `pip install -r requirements.txt` inside the venv
- The `project_start.bat` activates the venv before running
- Sandbox tests run using the venv Python, not the system Python

This prevents dependency conflicts between generated projects.

## Telegram Commands

| Command | Description |
|---------|-------------|
| `/build` | Start the build wizard (mode selection, scope, etc.) |
| `/edit` | Edit an existing project |
| `/run` | Run a generated project |
| `/cancel` | Cancel the current build |
| `/cleanup` | Remove generated projects |
| `/status` | Show current build status |
| `/services` | Check service health (Ollama, Voicebox, ComfyUI) |
| `/help` | Show all available commands |

### Build Workflow via Telegram

1. Send `/build` to the bot
2. Select a build mode (FAST/AVERAGE/GOD MODE/...)
3. Answer setup questions (internet, scope, tests)
4. Type your project description
5. Bot shows the blueprint and asks for approval
6. Click "Approve" to start code generation
7. Bot streams progress (agent phases, files, errors)
8. Project is saved to `output/<project_name>/`

## Skills (Plugins)

| Skill | Description |
|-------|-------------|
| **builder** | Code generation with 5-agent pipeline |
| **chat** | General conversation with local LLMs |
| **audio** | Speech-to-text (Whisper) + TTS (Voicebox/Qwen3-TTS) |
| **comfyui** | AI image generation (Flux, Z_Turbo) |
| **knowledge** | Personal knowledge base with RAG (ChromaDB) |
| **websearch** | Web search via DuckDuckGo |
| **downloader** | File/Steam/YouTube downloads |
| **desktop** | Automated file organization |

## Supported Languages

The builder can generate projects in:
- Python, Rust, Go, TypeScript, JavaScript
- HTML/CSS/JS (browser apps)
- Java, C#, C++, and more

Each language has:
- Compile checks (mypy, cargo check, tsc, go build)
- Sandbox testing (auto-detect entry point, run, check output)
- Dependency installation (pip, npm, cargo, go mod)
- Error pattern analysis for smart repairs

## Project Structure

```
AI HUB/
  core/                     # Central application core
    config.py               # Configuration & env loading
    dispatcher.py           # Intent router (smart argument extraction)
    llm_client.py           # Ollama client (OpenAI-compatible API)
    telegram_gateway.py     # Telegram bot entry point
    telegram/               # Telegram integration
      builder.py            # Docker builder runner
      handlers/             # Command & callback handlers
  skills/                   # Modular skill plugins
    builder/                # Code generation engine
      engine/               # Core builder components
        agents.py           # 5-agent pipeline (Planner/Retriever/Coder/Executor/Critic)
        pipeline.py         # Build orchestration
        skeletons.py        # Skeleton generation & fill-in
        blueprint.py        # Architecture planning
        sandbox.py          # Project execution testing
        ...
    chat/                   # General conversation
    audio/                  # Speech-to-text + TTS
    comfyui/                # Image generation
    knowledge/              # RAG knowledge base
    websearch/              # Web search
    downloader/             # File downloads
    desktop/                # File organization
  output/                   # Generated projects
  main.py                   # Application entry point
  start.bat                 # Windows quick-start
  .env.example              # Configuration template
  requirements.txt          # Python dependencies
```

## Troubleshooting

**Ollama not responding:**
```bash
ollama serve
ollama list   # Check which models are available
```

**Docker build fails:**
```bash
docker build -f skills\builder\Dockerfile -t ai-cluster .
```

**Out of VRAM:**
- Use FAST mode (smaller models)
- Close other GPU applications
- Models are loaded sequentially - only one at a time

**Python project dependency errors:**
- The builder creates virtual environments automatically
- Check `output/<project>/requirements.txt` for invalid packages
- The builder will attempt to repair manifests automatically

**Build stuck on approval:**
- Click the Approve/Cancel buttons in Telegram
- Or use `/cancel` to abort
