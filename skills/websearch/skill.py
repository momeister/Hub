"""
skills/websearch/skill.py — Web Search Agent
==============================================
Searches the web using DuckDuckGo (no API key needed).
Falls back to a simple HTTP scrape if duckduckgo-search is not installed.

Zero VRAM — pure CPU/network operation.
Optional: can run inside Docker for network isolation.
"""

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

log = logging.getLogger("skill.websearch")

MAX_RESULTS = 10
DEFAULT_RESULTS = 5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ============================================================================
# DUCKDUCKGO SEARCH (primary method)
# ============================================================================

def _search_ddg(query: str, num_results: int = 5) -> list[dict]:
    """Search using duckduckgo-search library."""
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return []

    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=num_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", ""),
                })
    except Exception as e:
        log.warning(f"DuckDuckGo search failed: {e}")

    return results


# ============================================================================
# FALLBACK: DuckDuckGo HTML scrape (no dependencies)
# ============================================================================

def _search_ddg_html(query: str, num_results: int = 5) -> list[dict]:
    """Fallback: scrape DuckDuckGo HTML results."""
    encoded = urllib.parse.quote_plus(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning(f"DDG HTML fallback failed: {e}")
        return []

    results = []

    # Parse result blocks
    # DDG HTML has <a class="result__a" href="...">title</a>
    # and <a class="result__snippet">snippet</a>
    blocks = re.findall(
        r'<a\s+rel="nofollow"\s+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
        r'.*?<a\s+class="result__snippet"[^>]*>(.*?)</a>',
        html,
        re.DOTALL,
    )

    for href, title, snippet in blocks[:num_results]:
        # DDG wraps URLs in redirect links
        actual_url = href
        uddg = re.search(r'uddg=([^&]+)', href)
        if uddg:
            actual_url = urllib.parse.unquote(uddg.group(1))

        title_clean = re.sub(r'<[^>]+>', '', title).strip()
        snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()

        if title_clean and actual_url:
            results.append({
                "title": title_clean,
                "url": actual_url,
                "snippet": snippet_clean,
            })

    return results


# ============================================================================
# PAGE FETCH (for summarization)
# ============================================================================

def _fetch_page_text(url: str, max_chars: int = 5000) -> str:
    """Fetch a web page and extract text content."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return f"[Failed to fetch: {e}]"

    # Strip scripts, styles, tags
    html = re.sub(r'<(script|style|nav|footer|header)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()

    return text[:max_chars]


# ============================================================================
# SEARCH (with optional LLM summarization)
# ============================================================================

def search(query: str, num_results: int = 5, fetch_content: bool = False) -> list[dict]:
    """
    Search the web. Tries duckduckgo-search library first, falls back to HTML scrape.

    Args:
        query: Search query
        num_results: Number of results (1-10)
        fetch_content: If True, also fetches page content for top results

    Returns: list of {title, url, snippet, content?}
    """
    num_results = max(1, min(num_results, MAX_RESULTS))

    # Try primary method
    results = _search_ddg(query, num_results)

    # Fallback
    if not results:
        log.info("DDG library unavailable, using HTML fallback")
        results = _search_ddg_html(query, num_results)

    # Optionally fetch page content for top results
    if fetch_content and results:
        for r in results[:3]:  # Only top 3 to avoid slowness
            try:
                r["content"] = _fetch_page_text(r["url"], max_chars=3000)
            except Exception:
                r["content"] = ""

    return results


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run(
    request: str = "",
    num_results: int = 5,
    **kwargs,
) -> dict:
    """
    Main entry point for the web search skill.

    Returns: {success, message, results}
    """
    query = request or kwargs.get("query", "")
    if not query.strip():
        return {"success": False, "message": "No search query provided.", "results": []}

    log.info(f"Web search: {query}")
    results = search(query, num_results=num_results)

    if not results:
        return {
            "success": True,
            "message": f"No results found for: {query}",
            "results": [],
        }

    # Format results
    result_lines = []
    for i, r in enumerate(results, 1):
        result_lines.append(
            f"*[{i}] {r['title']}*\n"
            f"   {r['url']}\n"
            f"   _{r['snippet'][:200]}_"
        )

    msg = f"*Web Search:* {query}\n\n" + "\n\n".join(result_lines)

    return {
        "success": True,
        "message": msg,
        "results": results,
    }
