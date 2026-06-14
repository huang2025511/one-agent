"""Document search skill — RAG-style retrieval from user documents.

Supports: PDF, Markdown, TXT
Storage: SQLite FTS5 (full-text search)
No vector database required.
"""

from __future__ import annotations

import os
import re
import sqlite3
import logging
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class DocumentStore:
    """SQLite FTS5 backed document store for RAG."""

    def __init__(self, db_path: str = "data/memory/docs.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
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
        # Remove existing doc with same name
        cur = self._conn.execute("SELECT id FROM documents WHERE name = ?", (name,))
        row = cur.fetchone()
        if row:
            doc_id = row["id"]
            self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))

        cur = self._conn.execute(
            "INSERT INTO documents (name, type, size_bytes, created_at) VALUES (?, 'txt', ?, unixepoch())",
            (name, len(content.encode()))
        )
        doc_id = cur.lastrowid

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

        for i, chunk in enumerate(chunks):
            self._conn.execute(
                "INSERT INTO doc_chunks (doc_id, chunk_idx, content) VALUES (?, ?, ?)",
                (doc_id, i, chunk)
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
            try:
                import subprocess
                result = subprocess.run(['pdftotext', '-layout', str(path), '-'],
                                      capture_output=True, text=True, timeout=30)
                content = result.stdout
            except Exception:
                # Fallback: try reading as text
                content = path.read_text(encoding='utf-8', errors='ignore')
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