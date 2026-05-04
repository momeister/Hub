"""
skills/builder/engine/knowledge_base.py - Persistent Semantic Knowledge Base
==============================================================================
System 4: ChromaDB + sentence-transformers for semantic similarity search
over past projects, errors, and solutions. Complements the Knowledge Graph
with vector-based retrieval.

All operations are No-Ops if chromadb or sentence-transformers are not installed.
Embedding model runs on CPU (no VRAM usage).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skills.builder.engine.context import blog

# ---------------------------------------------------------------------------
# Graceful imports — No-Op if dependencies missing
# ---------------------------------------------------------------------------
_chromadb_available = False
_st_available = False
_chromadb = None
_SentenceTransformer = None

try:
    import chromadb as _chromadb
    _chromadb_available = True
except ImportError:
    pass

try:
    from sentence_transformers import SentenceTransformer as _SentenceTransformer
    _st_available = True
except ImportError:
    pass

KB_PATH = str(Path(os.path.expanduser("~/.builder_knowledge/vectorstore")).resolve())

# Embedding model — 22 MB, runs on CPU
_EMBED_MODEL_NAME = "all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# Knowledge Base class
# ---------------------------------------------------------------------------
class BuilderKnowledgeBase:
    """Persistent semantic knowledge base for the Builder.

    Stores experiences from past builds as vectors in ChromaDB.
    Uses sentence-transformers for local embeddings (CPU only).
    """

    def __init__(self):
        """Initialize ChromaDB and sentence-transformer model.

        Sets self.available = False if dependencies are missing.
        """
        self.available = False
        self._client = None
        self._embed_model = None
        self._project_collection = None
        self._error_collection = None
        self._pattern_collection = None

        if not _chromadb_available:
            blog.info("Knowledge Base: chromadb not installed, KB features disabled")
            return

        if not _st_available:
            blog.info("Knowledge Base: sentence-transformers not installed, KB features disabled")
            return

        try:
            # Initialize ChromaDB (persistent)
            os.makedirs(KB_PATH, exist_ok=True)
            self._client = _chromadb.PersistentClient(path=KB_PATH)

            # Initialize embedding model on CPU
            self._embed_model = _SentenceTransformer(
                _EMBED_MODEL_NAME,
                device="cpu",
            )

            # Create/get collections
            self._project_collection = self._client.get_or_create_collection(
                name="project_experiences",
                metadata={"description": "Past build experiences with outcomes"},
            )
            self._error_collection = self._client.get_or_create_collection(
                name="error_solutions",
                metadata={"description": "Error-fix pairs from builds"},
            )
            self._pattern_collection = self._client.get_or_create_collection(
                name="code_patterns",
                metadata={"description": "Successful code snippets with context"},
            )

            self.available = True
            blog.info(f"Knowledge Base initialized at {KB_PATH}")

        except Exception as exc:
            blog.warning(f"Knowledge Base init failed: {exc}")
            self.available = False

    def _embed(self, text: str) -> list[float]:
        """Generate embedding for a text string."""
        if not self._embed_model:
            return []
        return self._embed_model.encode(text, show_progress_bar=False).tolist()

    # -------------------------------------------------------------------
    # Store methods
    # -------------------------------------------------------------------
    def store_project_experience(
        self,
        goal: str,
        language: str,
        framework: str,
        success: bool,
        key_decisions: list[str],
        problems_encountered: list[str],
        solutions_applied: list[str],
        sample_files: dict[str, str] | None = None,
    ) -> None:
        """Store a project experience as a vector + metadata."""
        if not self.available or not self._project_collection:
            return

        try:
            # Build embedding text
            embed_text = f"{goal} {language} {framework} " + " ".join(key_decisions)

            # Build metadata
            meta = {
                "goal": goal[:500],
                "language": language,
                "framework": framework,
                "success": success,
                "key_decisions": json.dumps(key_decisions[:10]),
                "problems_encountered": json.dumps(problems_encountered[:10]),
                "solutions_applied": json.dumps(solutions_applied[:10]),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            # Truncate sample files
            if sample_files:
                samples = {}
                for fname, content in list(sample_files.items())[:3]:
                    lines = content.splitlines()[:200]
                    samples[fname] = "\n".join(lines)
                meta["sample_files"] = json.dumps(samples)[:5000]

            doc_id = f"proj_{hash(goal + language) % 100000}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

            self._project_collection.add(
                documents=[embed_text],
                metadatas=[meta],
                ids=[doc_id],
            )

            blog.info(f"KB: Stored project experience (success={success})")

        except Exception as exc:
            blog.warning(f"KB: Failed to store project experience: {exc}")

    def store_error_solution(
        self,
        error_type: str,
        error_context: str,
        error_message: str,
        solution: str,
        language: str,
        worked: bool,
    ) -> None:
        """Store an error-solution pair."""
        if not self.available or not self._error_collection:
            return

        try:
            embed_text = f"{error_type} {error_context} {error_message}"

            meta = {
                "error_type": error_type,
                "error_context": error_context[:300],
                "error_message": error_message[:500],
                "solution": solution[:1000],
                "language": language,
                "worked": worked,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            doc_id = f"err_{hash(error_message) % 100000}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

            self._error_collection.add(
                documents=[embed_text],
                metadatas=[meta],
                ids=[doc_id],
            )

        except Exception as exc:
            blog.warning(f"KB: Failed to store error solution: {exc}")

    # -------------------------------------------------------------------
    # Retrieval methods
    # -------------------------------------------------------------------
    def retrieve_similar_experiences(
        self,
        query: str,
        language: str = "",
        n_results: int = 3,
    ) -> list[dict]:
        """Semantic search for similar past projects."""
        if not self.available or not self._project_collection:
            return []

        try:
            # Build where filter
            where_filter = None
            if language:
                where_filter = {"language": language}

            results = self._project_collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter,
            )

            experiences = []
            if results and results.get("metadatas"):
                for i, meta in enumerate(results["metadatas"][0]):
                    dist = results.get("distances", [[]])[0][i] if results.get("distances") else 1.0
                    similarity = max(0, 1.0 - dist)  # Convert distance to similarity

                    experiences.append({
                        "goal": meta.get("goal", ""),
                        "language": meta.get("language", ""),
                        "framework": meta.get("framework", ""),
                        "success": meta.get("success", False),
                        "similarity_score": round(similarity, 2),
                        "key_decisions": json.loads(meta.get("key_decisions", "[]")),
                        "problems_encountered": json.loads(meta.get("problems_encountered", "[]")),
                        "solutions_applied": json.loads(meta.get("solutions_applied", "[]")),
                    })

            return experiences

        except Exception as exc:
            blog.warning(f"KB: Similarity search failed: {exc}")
            return []

    def retrieve_solutions_for_error(
        self,
        error_message: str,
        language: str = "",
        n_results: int = 3,
    ) -> list[dict]:
        """Semantic search for solutions to similar errors."""
        if not self.available or not self._error_collection:
            return []

        try:
            where_filter = None
            if language:
                where_filter = {"language": language}

            results = self._error_collection.query(
                query_texts=[error_message],
                n_results=n_results,
                where=where_filter,
            )

            solutions = []
            if results and results.get("metadatas"):
                for i, meta in enumerate(results["metadatas"][0]):
                    dist = results.get("distances", [[]])[0][i] if results.get("distances") else 1.0
                    similarity = max(0, 1.0 - dist)

                    solutions.append({
                        "error_type": meta.get("error_type", ""),
                        "error_message": meta.get("error_message", ""),
                        "solution": meta.get("solution", ""),
                        "language": meta.get("language", ""),
                        "worked": meta.get("worked", False),
                        "similarity_score": round(similarity, 2),
                    })

            return solutions

        except Exception as exc:
            blog.warning(f"KB: Error solution search failed: {exc}")
            return []

    def build_context_for_new_project(
        self, goal: str, language: str, framework: str,
    ) -> str:
        """Build a formatted context block for the Planner prompt.

        Returns an empty string if no relevant experiences are found.
        """
        if not self.available:
            return ""

        parts: list[str] = []

        # Find similar projects
        experiences = self.retrieve_similar_experiences(
            query=f"{goal} {language} {framework}",
            language=language,
            n_results=3,
        )

        if experiences:
            parts.append("=== ERFAHRUNGEN AUS AEHNLICHEN PROJEKTEN ===")
            for i, exp in enumerate(experiences, 1):
                success_icon = "Erfolgreich" if exp["success"] else "Fehlgeschlagen"
                parts.append(
                    f"\nAehnliches Projekt {i} (Aehnlichkeit: {exp['similarity_score']:.0%}): "
                    f'"{exp["goal"]}"'
                )
                parts.append(
                    f"Sprache: {exp['language']}/{exp['framework']} | "
                    f"Ergebnis: {success_icon}"
                )
                if exp.get("key_decisions"):
                    parts.append(f"Wichtige Entscheidungen: {', '.join(exp['key_decisions'][:3])}")
                if exp.get("problems_encountered"):
                    parts.append(f"Probleme: {', '.join(exp['problems_encountered'][:3])}")
                if exp.get("solutions_applied"):
                    parts.append(f"Loesungen: {', '.join(exp['solutions_applied'][:3])}")

        # Find common errors for this language/framework
        error_solutions = self.retrieve_solutions_for_error(
            error_message=f"common errors {language} {framework}",
            language=language,
            n_results=5,
        )

        worked_solutions = [s for s in error_solutions if s.get("worked")]
        if worked_solutions:
            parts.append(f"\nBEKANNTE FALLSTRICKE fuer {language}/{framework}:")
            for sol in worked_solutions[:5]:
                parts.append(
                    f"  - [{sol['error_type']}]: {sol['solution'][:100]}"
                )

        return "\n".join(parts) if parts else ""

    def get_memory_usage_mb(self) -> float:
        """Return approximate RAM usage of the knowledge base in MB."""
        if not self.available:
            return 0.0

        try:
            import psutil
            process = psutil.Process(os.getpid())
            return process.memory_info().rss / (1024 * 1024)
        except ImportError:
            return 0.0


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_kb_instance: BuilderKnowledgeBase | None = None


def get_knowledge_base() -> BuilderKnowledgeBase:
    """Return the singleton KB instance (lazy init)."""
    global _kb_instance
    if _kb_instance is None:
        _kb_instance = BuilderKnowledgeBase()
    return _kb_instance
