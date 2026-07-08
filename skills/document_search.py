"""Document search skill — RAG-style retrieval from user documents.

Supports: PDF, Markdown, TXT
Storage: SQLite FTS5 (full-text search)
No vector database required.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List

from core.db import create_sqlite_connection
from core.security import is_path_within_any

logger = logging.getLogger(__name__)


class DocumentStore:
    """SQLite FTS5 backed document store for RAG."""

    def __init__(self, db_path: str = "data/memory/docs.db"):
        self._conn = create_sqlite_connection(db_path)
        # Thread-safe access: ingest_file runs in a thread pool via
        # run_in_executor while search/list run on the event loop thread.
        self._write_lock = threading.Lock()
        self._migrate()

    def _migrate(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                path TEXT,
                type TEXT DEFAULT 'txt',
                size_bytes INTEGER DEFAULT 0,
                chunk_count INTEGER DEFAULT 0,
                created_at REAL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks USING fts5(
                doc_id, chunk_idx, content,
                tokenize='unicode61'
            );
        """)
        self._conn.commit()

    def ingest_text(self, name: str, content: str, chunk_size: int = 1000) -> int:
        """Ingest plain text, split into chunks. Returns chunk count."""
        # Split into chunks (by paragraph boundaries when possible)
        paragraphs = re.split(r'\n\s*\n', content)
        chunks = []
        current = ""
        for p in paragraphs:
            if len(current) + len(p) < chunk_size:
                current += p + "\n\n"
            else:
                if current.strip():
                    chunks.append(current.strip())
                current = p + "\n\n"
        if current.strip():
            chunks.append(current.strip())

        with self._write_lock:
            # Remove existing doc with same name
            cur = self._conn.execute("SELECT id FROM documents WHERE name = ?", (name,))
            row = cur.fetchone()
            if row:
                doc_id = row["id"]
                self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                self._conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))

            cur = self._conn.execute(
                "INSERT INTO documents (name, type, size_bytes, created_at) VALUES (?, 'txt', ?, CAST(strftime('%s','now') AS REAL))",
                (name, len(content.encode()))
            )
            doc_id = cur.lastrowid

            rows = [(doc_id, i, chunk) for i, chunk in enumerate(chunks)]
            self._conn.executemany(
                "INSERT INTO doc_chunks (doc_id, chunk_idx, content) VALUES (?, ?, ?)",
                rows
            )

            self._conn.execute(
                "UPDATE documents SET chunk_count = ? WHERE id = ?",
                (len(chunks), doc_id)
            )
            self._conn.commit()
        return len(chunks)

    def ingest_file(self, filepath: str) -> int:
        """Ingest a file (PDF, MD, TXT). Returns chunk count."""
        path = Path(filepath)
        name = path.name
        ext = path.suffix.lower()

        if ext == '.pdf':
            # Try multiple PDF parsing approaches
            content = ""
            # 1. Try pdftotext (system command)
            try:
                import subprocess
                result = subprocess.run(
                    ['pdftotext', '-layout', str(path), '-'],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0 and result.stdout.strip():
                    content = result.stdout
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                logger.debug("pdftotext failed: %s", exc)

            # 2. Try PyPDF2
            if not content.strip():
                try:
                    from PyPDF2 import PdfReader
                    reader = PdfReader(str(path))
                    content = "\n".join(
                        page.extract_text() or "" for page in reader.pages
                    )
                except (ImportError, OSError, ValueError) as exc:
                    logger.debug("PyPDF2 parse failed: %s", exc)
                    pass

            # 3. Fallback: read raw text
            if not content.strip():
                try:
                    content = path.read_text(encoding='utf-8', errors='ignore')
                except (OSError, UnicodeDecodeError):
                    logger.warning("Failed to parse PDF: %s", name)
                    content = f"[无法解析 PDF: {name}]"
        else:
            content = path.read_text(encoding='utf-8', errors='ignore')

        return self.ingest_text(name, content)

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Full-text search across all documents. Returns matching chunks."""
        results = []
        try:
            cur = self._conn.execute(
                """SELECT d.name, c.chunk_idx, c.content,
                   snippet(doc_chunks, 2, '<b>', '</b>', '...', 32) as snippet
                   FROM doc_chunks c
                   JOIN documents d ON d.id = c.doc_id
                   WHERE doc_chunks MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (query, limit)
            )
            for row in cur.fetchall():
                results.append({
                    "document": row["name"],
                    "chunk": row["chunk_idx"],
                    "content": row["content"][:2000],
                    "snippet": row["snippet"],
                })
        except sqlite3.OperationalError:
            # FTS5 query syntax error - try simple LIKE fallback
            cur = self._conn.execute(
                """SELECT d.name, c.chunk_idx, c.content, c.content as snippet
                   FROM doc_chunks c
                   JOIN documents d ON d.id = c.doc_id
                   WHERE c.content LIKE ?
                   LIMIT ?""",
                (f"%{query}%", limit)
            )
            for row in cur.fetchall():
                results.append({
                    "document": row["name"],
                    "chunk": row["chunk_idx"],
                    "content": row["content"][:2000],
                    "snippet": row["content"][:200],
                })
        return results

    def list_documents(self) -> List[Dict[str, Any]]:
        """List all ingested documents."""
        cur = self._conn.execute(
            "SELECT id, name, type, size_bytes, chunk_count, created_at FROM documents ORDER BY created_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def delete_document(self, name: str) -> bool:
        """Delete a document and its chunks."""
        cur = self._conn.execute("SELECT id FROM documents WHERE name = ?", (name,))
        row = cur.fetchone()
        if not row:
            return False
        doc_id = row["id"]
        self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self._conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
        self._conn.commit()
        return True

    def close(self) -> None:
        """Close the database connection."""
        try:
            with self._write_lock:
                if self._conn:
                    self._conn.close()
                    self._conn = None
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()


# ------------------------------------------------------------------ skill handler factory

# Allowed roots for ingest (resolved absolute paths). Files outside these
# directories are rejected to prevent reading sensitive files like
# /etc/passwd or ~/.ssh/id_rsa via the skill interface.
_ALLOWED_INGEST_ROOTS = [
    Path("data/documents").resolve(),
    Path("data/uploads").resolve(),
    Path("data/docs").resolve(),
]
_ALLOWED_INGEST_EXTS = {".pdf", ".md", ".txt", ".markdown", ".text"}
_MAX_INGEST_BYTES = 50 * 1024 * 1024  # 50 MB


def _validate_ingest_path(path_str: str) -> Path:
    """Validate a user-supplied ingest path against allow-list and size limits.

    Returns the resolved Path if safe; raises ValueError otherwise.
    """
    if not path_str:
        raise ValueError("empty path")
    # Resolve to absolute, following symlinks (so symlink escapes are caught).
    try:
        resolved = Path(path_str).resolve(strict=True)
    except FileNotFoundError:
        raise ValueError(f"file not found: {path_str}")
    except OSError as exc:
        raise ValueError(f"cannot resolve path: {exc}")

    # Extension whitelist
    if resolved.suffix.lower() not in _ALLOWED_INGEST_EXTS:
        raise ValueError(
            f"unsupported file type: {resolved.suffix} "
            f"(allowed: {', '.join(sorted(_ALLOWED_INGEST_EXTS))})"
        )

    # Directory containment check (strict, not startswith)
    if not is_path_within_any(resolved, _ALLOWED_INGEST_ROOTS):
        raise ValueError(
            f"path '{resolved}' is outside allowed ingest directories: "
            f"{', '.join(str(r) for r in _ALLOWED_INGEST_ROOTS)}"
        )

    # Must be a regular file
    if not resolved.is_file():
        raise ValueError(f"not a regular file: {resolved}")

    # Size limit
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat file: {exc}")
    if size > _MAX_INGEST_BYTES:
        raise ValueError(
            f"file too large: {size} bytes (max {_MAX_INGEST_BYTES} bytes)"
        )

    return resolved


def make_doc_search_handler(store):
    async def handler(args):
        action = args.get("action", "search")
        if action == "search":
            query = args.get("query", args.get("input", ""))
            if not query:
                return "请提供搜索关键词"
            results = store.search(query, limit=args.get("limit", 5))
            if not results:
                return f"未找到与 '{query}' 相关的文档内容"
            lines = [f"文档搜索结果（{query}）："]
            for r in results:
                lines.append(f"\n📄 {r['document']} (chunk {r['chunk']})")
                lines.append(r['content'][:500])
            return "\n".join(lines)
        elif action == "list":
            docs = store.list_documents()
            if not docs:
                return "暂无已上传的文档"
            lines = ["已上传文档："]
            for d in docs:
                lines.append(f"  - {d['name']} ({d['type']}, {d['chunk_count']} chunks, {d['size_bytes']} bytes)")
            return "\n".join(lines)
        elif action == "ingest":
            import asyncio as _asyncio
            path = args.get("path", "")
            if not path:
                return "请提供文档路径"
            # Security: validate path before reading to prevent reading
            # sensitive files outside allowed directories.
            try:
                safe_path = _validate_ingest_path(path)
            except ValueError as exc:
                logger.warning("document_search ingest rejected: %s", exc)
                return f"拒绝摄入文档：{exc}"
            loop = _asyncio.get_running_loop()
            count = await loop.run_in_executor(None, store.ingest_file, str(safe_path))
            return f"已摄入文档，切分为 {count} 个 chunks"
        else:
            return f"未知操作: {action}，支持: search, list, ingest"
    return handler
