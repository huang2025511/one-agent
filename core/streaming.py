"""Streaming Response — SSE-based incremental output.

Provides real-time streaming responses for better UX:
- Server-Sent Events (SSE) for browser/HTTP clients
- Incremental token output (typewriter effect)
- Token buffering with configurable chunk size
- Cancellation support
- Progress indicators for long operations

The streaming system integrates with:
- LLM providers that support streaming
- Gateway/WebSocket for real-time delivery
- CLI for terminal-based streaming
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StreamChunk:
    """A chunk of streamed content."""
    content: str
    chunk_type: str = "text"  # text, code, markdown, thinking, tool_call, error
    is_final: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class StreamingBuffer:
    """Buffer for streaming content with chunking.

    Accumulates tokens and yields chunks when:
    - A minimum size is reached
    - A delimiter is encountered (punctuation, code block)
    - A timeout is reached (force flush)
    """

    def __init__(
        self,
        min_chunk_size: int = 10,
        max_chunk_size: int = 100,
        flush_timeout: float = 0.5,
    ) -> None:
        self._min_chunk_size = min_chunk_size
        self._max_chunk_size = max_chunk_size
        self._flush_timeout = flush_timeout
        self._buffer = ""
        self._last_flush = time.time()

    def add(self, text: str) -> List[StreamChunk]:
        """Add text to buffer and return any chunks ready to flush."""
        self._buffer += text
        chunks = []

        # Check if we should flush
        while len(self._buffer) >= self._min_chunk_size:
            # Try to find a good break point
            break_point = self._find_break_point()

            if break_point > 0:
                chunk_text = self._buffer[:break_point]
                self._buffer = self._buffer[break_point:]
                chunks.append(StreamChunk(content=chunk_text))
                self._last_flush = time.time()
            elif len(self._buffer) >= self._max_chunk_size:
                # Force flush at max size
                chunk_text = self._buffer[:self._max_chunk_size]
                self._buffer = self._buffer[self._max_chunk_size:]
                chunks.append(StreamChunk(content=chunk_text))
                self._last_flush = time.time()
            else:
                break

        return chunks

    def _find_break_point(self) -> int:
        """Find a good point to break the buffer."""
        # Delimiters in order of preference
        delimiters = [
            ("\n\n", 2),  # Paragraph break
            ("\n", 1),  # Line break
            (". ", 2),  # Sentence end
            ("。", 1),  # Chinese sentence end
            (" ", 1),  # Word boundary
        ]

        for delim, offset in delimiters:
            pos = self._buffer.rfind(delim, 0, self._max_chunk_size)
            if pos > self._min_chunk_size:
                return pos + offset

        # No good break point found
        return 0

    def flush(self) -> List[StreamChunk]:
        """Force flush the buffer."""
        chunks = []
        if self._buffer:
            chunks.append(StreamChunk(content=self._buffer, is_final=True))
            self._buffer = ""
            self._last_flush = time.time()
        return chunks

    def is_empty(self) -> bool:
        return len(self._buffer) == 0


class SSEFormatter:
    """Format streaming content as Server-Sent Events."""

    @staticmethod
    def format(chunk: StreamChunk) -> str:
        """Format a chunk as SSE data."""
        data = {
            "content": chunk.content,
            "type": chunk.chunk_type,
            "is_final": chunk.is_final,
        }
        if chunk.metadata:
            data["metadata"] = chunk.metadata

        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    @staticmethod
    def format_comment(message: str) -> str:
        """Format a comment (used for heartbeats)."""
        return f": {message}\n\n"

    @staticmethod
    def format_event(event_type: str, data: Dict[str, Any]) -> str:
        """Format a named event."""
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class StreamingResponse:
    """Manages a streaming response session.

    Handles:
    - Token buffering and chunking
    - SSE formatting
    - Cancellation
    - Progress tracking
    """

    def __init__(
        self,
        session_id: str,
        min_chunk_size: int = 10,
        max_chunk_size: int = 100,
    ) -> None:
        self._session_id = session_id
        self._buffer = StreamingBuffer(min_chunk_size, max_chunk_size)
        self._sse_formatter = SSEFormatter()
        self._cancelled = False
        self._chunks_sent = 0
        self._tokens_sent = 0
        self._started_at = time.time()
        self._is_complete = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        """Cancel the streaming response."""
        self._cancelled = True
        logger.info("Streaming session %s cancelled", self._session_id)

    def is_active(self) -> bool:
        """Check if streaming is still active."""
        return not self._cancelled and not self._is_complete

    async def send_text(self, text: str) -> List[str]:
        """Send text and return SSE-formatted chunks."""
        if self._cancelled:
            return []

        chunks = self._buffer.add(text)
        self._chunks_sent += len(chunks)
        self._tokens_sent += len(text)

        return [self._sse_formatter.format(chunk) for chunk in chunks]

    async def send_chunk(self, chunk: StreamChunk) -> str:
        """Send a pre-formatted chunk."""
        if self._cancelled:
            return ""

        self._chunks_sent += 1
        self._tokens_sent += len(chunk.content)

        return self._sse_formatter.format(chunk)

    async def flush(self) -> List[str]:
        """Force flush any buffered content."""
        chunks = self._buffer.flush()
        self._chunks_sent += len(chunks)
        return [self._sse_formatter.format(chunk) for chunk in chunks]

    async def complete(self, final_content: str = "") -> List[str]:
        """Mark streaming as complete and send final chunk."""
        self._is_complete = True

        # Flush remaining buffer
        flush_chunks = await self.flush()

        # Send completion event
        duration = time.time() - self._started_at
        completion_event = self._sse_formatter.format_event("complete", {
            "session_id": self._session_id,
            "duration": duration,
            "chunks_sent": self._chunks_sent,
            "tokens_sent": self._tokens_sent,
            "cancelled": self._cancelled,
        })

        return flush_chunks + [completion_event]

    async def send_heartbeat(self) -> str:
        """Send a heartbeat comment."""
        return self._sse_formatter.format_comment(f"heartbeat {time.time()}")

    def get_stats(self) -> Dict[str, Any]:
        """Get streaming statistics."""
        return {
            "session_id": self._session_id,
            "is_active": self.is_active(),
            "cancelled": self._cancelled,
            "chunks_sent": self._chunks_sent,
            "tokens_sent": self._tokens_sent,
            "duration": time.time() - self._started_at,
            "buffer_size": len(self._buffer._buffer),
        }


class StreamingAggregator:
    """Aggregates multiple streaming sources into one.

    Useful for parallel tool execution where each tool streams
    its own progress, but we want to merge into one response.
    """

    def __init__(self) -> None:
        self._streams: Dict[str, StreamingResponse] = {}

    def add_stream(self, stream_id: str, stream: StreamingResponse) -> None:
        """Add a streaming source."""
        self._streams[stream_id] = stream

    def remove_stream(self, stream_id: str) -> None:
        """Remove a streaming source."""
        self._streams.pop(stream_id, None)

    def cancel_all(self) -> None:
        """Cancel all active streams."""
        for stream in self._streams.values():
            stream.cancel()

    async def broadcast(self, text: str) -> Dict[str, str]:
        """Broadcast text to all streams."""
        results = {}
        for stream_id, stream in list(self._streams.items()):
            if stream.is_active():
                chunks = await stream.send_text(text)
                results[stream_id] = "".join(chunks)
        return results


class StreamingProcessor:
    """Process streamed LLM responses.

    Wraps an async iterator of tokens and produces formatted SSE chunks.
    Supports:
    - Token buffering
    - Code block detection
    - Markdown detection
    - Tool call detection
    """

    def __init__(
        self,
        min_chunk_size: int = 10,
        max_chunk_size: int = 100,
    ) -> None:
        self._buffer = StreamingBuffer(min_chunk_size, max_chunk_size)
        self._sse_formatter = SSEFormatter()
        self._in_code_block = False
        self._in_thinking = False

    async def process_tokens(
        self,
        token_iterator: AsyncIterator[str],
    ) -> AsyncIterator[str]:
        """Process tokens and yield SSE-formatted chunks."""
        async for token in token_iterator:
            # Determine chunk type
            chunk_type = self._detect_chunk_type(token)

            # Add to buffer
            chunks = self._buffer.add(token)
            for chunk in chunks:
                chunk.chunk_type = chunk_type
                yield self._sse_formatter.format(chunk)

        # Flush remaining
        for chunk in self._buffer.flush():
            yield self._sse_formatter.format(chunk)

    def _detect_chunk_type(self, token: str) -> str:
        """Detect the type of content based on token."""
        # Check for code blocks
        if "```" in token:
            self._in_code_block = not self._in_code_block
            return "code"

        if self._in_code_block:
            return "code"

        # Check for thinking/reasoning
        if token.strip().startswith(("(", "[", "**")):
            if "thinking" not in token.lower():
                return "thinking"

        # Check for tool calls
        if "tool" in token.lower() or "function" in token.lower():
            return "tool_call"

        return "text"


# Utility functions for CLI streaming
async def stream_to_terminal(
    stream: StreamingResponse,
    token_iterator: AsyncIterator[str],
    chunk_size: int = 20,
) -> str:
    """Stream response to terminal with typewriter effect."""
    import sys

    full_response = []

    async for token in token_iterator:
        if stream.cancelled:
            break

        # Print token immediately for responsiveness
        print(token, end="", flush=True)
        full_response.append(token)

        # Small delay for typewriter effect (configurable)
        if len(token.strip()) > 0:
            await asyncio.sleep(0.01)  # 10ms between tokens

    print()  # New line after streaming
    return "".join(full_response)


async def stream_with_progress(
    stream: StreamingResponse,
    token_iterator: AsyncIterator[str],
    progress_callback: Optional[Callable] = None,
) -> str:
    """Stream with progress updates."""
    import sys

    full_response = []
    last_progress_update = time.time()

    async for token in token_iterator:
        if stream.cancelled:
            break

        full_response.append(token)

        # Send progress update every 0.5 seconds
        now = time.time()
        if now - last_progress_update > 0.5 and progress_callback:
            progress_callback(len(full_response))
            last_progress_update = now

    # Final progress update
    if progress_callback:
        progress_callback(len(full_response), is_complete=True)

    return "".join(full_response)