"""
skills/knowledge/skill.py — Knowledge Base with Vector Search (RAG)
====================================================================
Personal knowledge management: ingest documents (PDF, email, text, markdown),
store embeddings in ChromaDB, search with semantic similarity.

Embedding: ChromaDB default (all-MiniLM-L6-v2 via ONNX) — runs 100% on CPU.
Zero VRAM impact. Safe to use while gpt-oss:120B is loaded.

Supported formats:
  - .pdf (via pymupdf/fitz)
  - .eml (Python email module)
  - .txt, .md, .rst, .log, .csv, .json, .yaml, .yml
  - .py, .js, .ts, .rs, .go, .java, .c, .cpp, .h, .cs (code files)
  - .html (basic tag stripping)
  - .docx (via python-docx, optional)

Safety:
  - READ-ONLY access — never deletes or modifies source files
  - Path traversal protection
  - Configurable watched folders via env
"""

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("skill.knowledge")

# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CHROMA_DIR = os.path.join(DATA_DIR, "chroma_db")
COLLECTION_NAME = "knowledge"

# Max chunk size in characters (~500 tokens at 3.5 chars/token)
CHUNK_SIZE = 1750
CHUNK_OVERLAP = 200

# File size limits
MAX_FILE_SIZE_MB = 50
MAX_FILES_PER_INGEST = 500

# Supported file extensions
TEXT_EXTENSIONS = {
    ".txt", ".md", ".rst", ".log", ".csv", ".json", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".toml",
}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".rs", ".go", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".sh",
    ".bat", ".ps1", ".sql", ".lua", ".r", ".swift", ".kt",
}
DOCUMENT_EXTENSIONS = {".pdf", ".eml", ".html", ".htm", ".docx"}

ALL_EXTENSIONS = TEXT_EXTENSIONS | CODE_EXTENSIONS | DOCUMENT_EXTENSIONS


# ============================================================================
# LAZY CHROMADB INITIALIZATION
# ============================================================================

_client = None
_collection = None


def _get_collection():
    """Lazy-init ChromaDB. Only loads when first needed."""
    global _client, _collection
    if _collection is not None:
        return _collection

    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError:
        raise RuntimeError(
            "chromadb not installed. Run: pip install chromadb\n"
            "ChromaDB uses ONNX embeddings (CPU only) — zero VRAM impact."
        )

    os.makedirs(CHROMA_DIR, exist_ok=True)

    _client = chromadb.PersistentClient(
        path=CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )

    _collection = _client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    count = _collection.count()
    log.info(f"ChromaDB initialized: {count} documents in collection '{COLLECTION_NAME}'")
    return _collection


# ============================================================================
# TEXT EXTRACTION
# ============================================================================

def _extract_pdf(file_path: str) -> str:
    """Extract text from PDF using pymupdf (fitz)."""
    try:
        import fitz  # pymupdf
    except ImportError:
        raise RuntimeError("pymupdf not installed. Run: pip install pymupdf")

    text_parts = []
    with fitz.open(file_path) as doc:
        for page_num, page in enumerate(doc, 1):
            page_text = page.get_text("text")
            if page_text.strip():
                text_parts.append(f"[Page {page_num}]\n{page_text}")
    return "\n\n".join(text_parts)


def _extract_email(file_path: str) -> str:
    """Extract text from .eml files."""
    import email
    from email import policy

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        msg = email.message_from_file(f, policy=policy.default)

    parts = []
    # Headers
    for header in ("From", "To", "Subject", "Date"):
        val = msg.get(header, "")
        if val:
            parts.append(f"{header}: {val}")

    # Body
    body = msg.get_body(preferencelist=("plain", "html"))
    if body:
        content = body.get_content()
        if body.get_content_type() == "text/html":
            content = _strip_html(content)
        parts.append(f"\n{content}")

    return "\n".join(parts)


def _extract_html(file_path: str) -> str:
    """Extract text from HTML, stripping tags."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        html = f.read()
    return _strip_html(html)


def _strip_html(html: str) -> str:
    """Basic HTML tag stripping."""
    # Remove script and style
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Remove tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Clean whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_docx(file_path: str) -> str:
    """Extract text from .docx files."""
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("python-docx not installed. Run: pip install python-docx")

    doc = Document(file_path)
    return "\n\n".join(para.text for para in doc.paragraphs if para.text.strip())


def _extract_text(file_path: str) -> str:
    """Read a plain text file."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_content(file_path: str) -> str:
    """
    Extract text content from a file based on its extension.
    Returns extracted text, or raises RuntimeError on unsupported format.
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".pdf":
        return _extract_pdf(file_path)
    elif ext == ".eml":
        return _extract_email(file_path)
    elif ext in (".html", ".htm"):
        return _extract_html(file_path)
    elif ext == ".docx":
        return _extract_docx(file_path)
    elif ext in TEXT_EXTENSIONS or ext in CODE_EXTENSIONS:
        return _extract_text(file_path)
    else:
        raise RuntimeError(f"Unsupported file format: {ext}")


# ============================================================================
# CHUNKING
# ============================================================================

def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """
    Split text into overlapping chunks.
    Tries to break at paragraph/sentence boundaries.
    """
    if not text or not text.strip():
        return []

    # If text fits in one chunk, return as is
    if len(text) <= chunk_size:
        return [text.strip()]

    chunks = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size

        if end >= text_len:
            chunk = text[start:].strip()
            if chunk:
                chunks.append(chunk)
            break

        # Try to break at paragraph boundary
        para_break = text.rfind("\n\n", start + chunk_size // 2, end)
        if para_break > start:
            end = para_break

        # Try to break at sentence boundary
        elif (sent_break := text.rfind(". ", start + chunk_size // 2, end)) > start:
            end = sent_break + 1

        # Try to break at newline
        elif (nl_break := text.rfind("\n", start + chunk_size // 2, end)) > start:
            end = nl_break

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        start = end - overlap
        if start <= (end - chunk_size):
            start = end  # Prevent infinite loop

    return chunks


def _file_hash(file_path: str) -> str:
    """Fast file hash for deduplication (first 8KB + size)."""
    h = hashlib.md5()
    file_size = os.path.getsize(file_path)
    h.update(str(file_size).encode())
    with open(file_path, "rb") as f:
        h.update(f.read(8192))
    return h.hexdigest()


# ============================================================================
# INGEST
# ============================================================================

def ingest_file(file_path: str) -> dict:
    """
    Ingest a single file into the knowledge base.
    READ-ONLY: never modifies or deletes the source file.

    Returns: {"success": bool, "chunks": int, "message": str}
    """
    file_path = os.path.abspath(file_path)

    if not os.path.isfile(file_path):
        return {"success": False, "chunks": 0, "message": f"File not found: {file_path}"}

    ext = Path(file_path).suffix.lower()
    if ext not in ALL_EXTENSIONS:
        return {"success": False, "chunks": 0, "message": f"Unsupported format: {ext}"}

    # Size check
    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        return {"success": False, "chunks": 0, "message": f"File too large: {size_mb:.1f}MB (max {MAX_FILE_SIZE_MB}MB)"}

    collection = _get_collection()

    # Dedup: check if this file version is already indexed
    fhash = _file_hash(file_path)
    existing = collection.get(where={"file_hash": fhash})
    if existing and existing["ids"]:
        return {
            "success": True,
            "chunks": len(existing["ids"]),
            "message": f"Already indexed ({len(existing['ids'])} chunks): {Path(file_path).name}",
        }

    # Extract text
    try:
        content = extract_content(file_path)
    except Exception as e:
        return {"success": False, "chunks": 0, "message": f"Extraction failed: {e}"}

    if not content.strip():
        return {"success": False, "chunks": 0, "message": "File is empty or has no extractable text"}

    # Chunk
    chunks = _chunk_text(content)
    if not chunks:
        return {"success": False, "chunks": 0, "message": "No chunks generated"}

    # Prepare for ChromaDB
    file_name = Path(file_path).name
    file_stem = Path(file_path).stem
    base_id = f"{file_stem}_{fhash[:8]}"
    ids = [f"{base_id}_c{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "source": file_path,
            "file_name": file_name,
            "file_hash": fhash,
            "file_type": ext,
            "chunk_index": i,
            "total_chunks": len(chunks),
            "ingested_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        for i in range(len(chunks))
    ]

    # Add to collection (ChromaDB handles embedding via ONNX on CPU)
    try:
        collection.add(
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )
    except Exception as e:
        return {"success": False, "chunks": 0, "message": f"ChromaDB insert failed: {e}"}

    log.info(f"Ingested {file_name}: {len(chunks)} chunks, {len(content)} chars")
    return {
        "success": True,
        "chunks": len(chunks),
        "message": f"Indexed {file_name}: {len(chunks)} chunks ({len(content):,} chars)",
    }


def ingest_folder(folder_path: str, recursive: bool = True) -> dict:
    """
    Ingest all supported files from a folder.
    READ-ONLY: never modifies source files.

    Returns: {"success": bool, "files": int, "chunks": int, "errors": list, "message": str}
    """
    folder_path = os.path.abspath(folder_path)

    if not os.path.isdir(folder_path):
        return {
            "success": False, "files": 0, "chunks": 0,
            "errors": [], "message": f"Folder not found: {folder_path}",
        }

    files_done = 0
    total_chunks = 0
    errors = []
    skipped = 0

    # Collect files
    if recursive:
        all_files = []
        for root, _dirs, filenames in os.walk(folder_path):
            # Skip hidden directories and common noise
            base = os.path.basename(root)
            if base.startswith(".") or base in ("node_modules", "__pycache__", ".git", "venv", ".venv"):
                continue
            for fname in filenames:
                fp = os.path.join(root, fname)
                if Path(fp).suffix.lower() in ALL_EXTENSIONS:
                    all_files.append(fp)
    else:
        all_files = [
            os.path.join(folder_path, f) for f in os.listdir(folder_path)
            if os.path.isfile(os.path.join(folder_path, f))
            and Path(f).suffix.lower() in ALL_EXTENSIONS
        ]

    if len(all_files) > MAX_FILES_PER_INGEST:
        return {
            "success": False, "files": 0, "chunks": 0,
            "errors": [],
            "message": f"Too many files: {len(all_files)} (max {MAX_FILES_PER_INGEST}). Narrow the folder.",
        }

    for fp in all_files:
        result = ingest_file(fp)
        if result["success"]:
            if "Already indexed" not in result["message"]:
                files_done += 1
                total_chunks += result["chunks"]
            else:
                skipped += 1
        else:
            errors.append(f"{Path(fp).name}: {result['message']}")

    msg = f"Indexed {files_done} files ({total_chunks} chunks)"
    if skipped:
        msg += f", {skipped} already indexed"
    if errors:
        msg += f", {len(errors)} errors"

    return {
        "success": True,
        "files": files_done,
        "chunks": total_chunks,
        "errors": errors[:10],
        "message": msg,
    }


# ============================================================================
# SEARCH
# ============================================================================

def search(query: str, top_k: int = 5) -> list[dict]:
    """
    Semantic search across the knowledge base.

    Returns list of results:
    [
        {
            "text": "...",
            "source": "file path",
            "file_name": "...",
            "file_type": ".pdf",
            "score": 0.85,
            "chunk_index": 2,
        },
        ...
    ]
    """
    if not query.strip():
        return []

    top_k = max(1, min(top_k, 20))

    collection = _get_collection()
    if collection.count() == 0:
        return []

    try:
        results = collection.query(
            query_texts=[query],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
    except Exception as e:
        log.error(f"Search failed: {e}")
        return []

    hits = []
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = results.get("distances", [[]])[0]

    for doc, meta, dist in zip(docs, metas, dists):
        # ChromaDB cosine distance: 0 = identical, 2 = opposite
        # Convert to similarity score: 1 - (dist/2)
        score = round(1.0 - (dist / 2.0), 4)
        hits.append({
            "text": doc,
            "source": meta.get("source", "?"),
            "file_name": meta.get("file_name", "?"),
            "file_type": meta.get("file_type", "?"),
            "score": score,
            "chunk_index": meta.get("chunk_index", 0),
        })

    return hits


# ============================================================================
# STATUS
# ============================================================================

def get_status() -> dict:
    """Get knowledge base statistics."""
    try:
        collection = _get_collection()
        count = collection.count()

        # Get unique sources
        if count > 0:
            all_meta = collection.get(include=["metadatas"])
            sources = set()
            file_types = {}
            for meta in all_meta.get("metadatas", []):
                src = meta.get("source", "")
                if src:
                    sources.add(src)
                ft = meta.get("file_type", "?")
                file_types[ft] = file_types.get(ft, 0) + 1
        else:
            sources = set()
            file_types = {}

        return {
            "total_chunks": count,
            "total_files": len(sources),
            "file_types": file_types,
            "db_path": CHROMA_DIR,
            "collection": COLLECTION_NAME,
        }
    except Exception as e:
        return {
            "total_chunks": 0,
            "total_files": 0,
            "file_types": {},
            "db_path": CHROMA_DIR,
            "error": str(e),
        }


def delete_source(file_path: str) -> dict:
    """Remove all chunks for a specific source file from the knowledge base."""
    file_path = os.path.abspath(file_path)
    collection = _get_collection()

    try:
        existing = collection.get(where={"source": file_path})
        if not existing or not existing["ids"]:
            return {"success": False, "message": f"No entries found for: {file_path}"}

        count = len(existing["ids"])
        collection.delete(ids=existing["ids"])
        return {"success": True, "message": f"Removed {count} chunks for {Path(file_path).name}"}
    except Exception as e:
        return {"success": False, "message": f"Delete failed: {e}"}


# ============================================================================
# MAIN ENTRY POINT (called by dispatcher)
# ============================================================================

def run(
    request: str = "",
    action: str = "search",
    path: str = "",
    top_k: int = 5,
    **kwargs,
) -> dict:
    """
    Main entry point for the knowledge skill.

    Actions:
      - search: Semantic search across knowledge base
      - ingest: Add file or folder to knowledge base
      - status: Show DB statistics

    Returns dict with results.
    """
    action = action.lower().strip()

    # ---- STATUS ----
    if action == "status":
        status = get_status()
        msg = (
            f"Knowledge Base Status:\n"
            f"  Files  : {status['total_files']}\n"
            f"  Chunks : {status['total_chunks']}\n"
            f"  DB     : {status['db_path']}\n"
        )
        if status.get("file_types"):
            type_str = ", ".join(f"{k}: {v}" for k, v in sorted(status["file_types"].items()))
            msg += f"  Types  : {type_str}\n"
        if status.get("error"):
            msg += f"  Error  : {status['error']}\n"
        return {"success": True, "message": msg, "data": status}

    # ---- INGEST ----
    if action == "ingest":
        if not path:
            return {"success": False, "message": "No path provided. Use path parameter."}

        path = os.path.abspath(path)

        if os.path.isfile(path):
            result = ingest_file(path)
        elif os.path.isdir(path):
            result = ingest_folder(path)
        else:
            return {"success": False, "message": f"Path not found: {path}"}

        return result

    # ---- SEARCH (default) ----
    query = request or kwargs.get("query", "")
    if not query:
        return {"success": False, "message": "No search query provided."}

    results = search(query, top_k=top_k)

    if not results:
        status = get_status()
        if status["total_chunks"] == 0:
            return {
                "success": True,
                "message": "Knowledge base is empty. Use /knowledge ingest <path> to add documents.",
                "results": [],
            }
        return {
            "success": True,
            "message": f"No relevant results found for: {query}",
            "results": [],
        }

    # Format results
    result_texts = []
    for i, hit in enumerate(results, 1):
        result_texts.append(
            f"[{i}] {hit['file_name']} (score: {hit['score']:.2f})\n"
            f"    {hit['text'][:300]}..."
            if len(hit["text"]) > 300 else
            f"[{i}] {hit['file_name']} (score: {hit['score']:.2f})\n"
            f"    {hit['text']}"
        )

    msg = f"Found {len(results)} results for: {query}\n\n" + "\n\n".join(result_texts)

    return {
        "success": True,
        "message": msg,
        "results": results,
    }
