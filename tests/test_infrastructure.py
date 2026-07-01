"""Tests for infrastructure features: task scheduler, context compressor, streaming, webhooks."""

import asyncio
import json
import tempfile
import time


class TestAsyncTaskScheduler:
    """Test async task scheduler."""

    def test_task_creation(self, tmp_path):
        """Test creating and storing tasks."""
        from core.task_scheduler import AsyncTaskScheduler, TaskStatus

        db_path = str(tmp_path / "tasks.db")
        scheduler = AsyncTaskScheduler(db_path=db_path, max_concurrent=3)

        async def dummy_task(**args):
            return {"result": "ok", "args": args}

        scheduler.register("dummy", dummy_task)

        task_id = asyncio.run(scheduler.schedule_delayed(
            "dummy",
            delay_seconds=1,
            args={"value": 42},
            name="test-task",
        ))

        task = scheduler.get_task(task_id)
        assert task is not None
        assert task.name == "test-task"
        assert task.status == TaskStatus.PENDING.value
        assert task.func_name == "dummy"

    def test_cancel_task(self, tmp_path):
        """Test cancelling a task."""
        from core.task_scheduler import AsyncTaskScheduler, TaskStatus

        db_path = str(tmp_path / "tasks.db")
        scheduler = AsyncTaskScheduler(db_path=db_path)

        async def dummy_task():
            return "done"

        scheduler.register("cancel_test", dummy_task)

        task_id = asyncio.run(scheduler.schedule_delayed(
            "cancel_test",
            delay_seconds=60,
        ))

        cancelled = asyncio.run(scheduler.cancel(task_id))
        assert cancelled is True

        task = scheduler.get_task(task_id)
        assert task.status == TaskStatus.CANCELLED.value

    def test_list_tasks(self, tmp_path):
        """Test listing tasks."""
        from core.task_scheduler import AsyncTaskScheduler

        db_path = str(tmp_path / "tasks.db")
        scheduler = AsyncTaskScheduler(db_path=db_path)

        async def dummy_task():
            return "done"

        scheduler.register("list_test", dummy_task)

        asyncio.run(scheduler.schedule_background("list_test", name="bg-1"))
        asyncio.run(scheduler.schedule_background("list_test", name="bg-2"))

        tasks = scheduler.list_tasks()
        assert len(tasks) >= 2

    def test_schedule_at(self, tmp_path):
        """Test scheduling a task at specific time."""
        from core.task_scheduler import AsyncTaskScheduler

        db_path = str(tmp_path / "tasks.db")
        scheduler = AsyncTaskScheduler(db_path=db_path)

        async def dummy_task():
            return "done"

        scheduler.register("at_test", dummy_task)

        future_time = time.time() + 3600  # 1 hour from now
        task_id = asyncio.run(scheduler.schedule_at(
            "at_test",
            run_at=future_time,
        ))

        task = scheduler.get_task(task_id)
        assert task is not None
        assert task.task_type == "one_time"
        assert abs(task.run_at - future_time) < 1


class TestContextCompressor:
    """Test context window compression."""

    def test_no_compression_needed(self):
        """Test that short conversations aren't compressed."""
        from core.context_compressor import ContextCompressor

        compressor = ContextCompressor(max_tokens=10000)

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        compressed, summary = compressor.compress(messages)

        assert summary == "no compression needed"
        assert len(compressed) == 2

    def test_compression_reduces_size(self):
        """Test that long conversations are compressed."""
        from core.context_compressor import ContextCompressor

        compressor = ContextCompressor(max_tokens=500)

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
        ]
        # Add many messages to exceed token budget
        for i in range(50):
            messages.append({"role": "user", "content": f"This is message number {i} with some extra content to make it longer than expected."})
            messages.append({"role": "assistant", "content": f"This is response number {i} with more content to ensure the conversation exceeds the token limit."})

        compressed, summary = compressor.compress(messages)

        # Should have compressed significantly
        assert "compressed" in summary
        assert len(compressed) < len(messages)

    def test_preserve_system_messages(self):
        """Test that system messages are preserved."""
        from core.context_compressor import ContextCompressor

        compressor = ContextCompressor(max_tokens=200, preserve_system=True)

        messages = [
            {"role": "system", "content": "IMPORTANT SYSTEM RULE."},
            {"role": "user", "content": "A"},
        ] * 30

        compressed, _ = compressor.compress(messages)

        # System message should be preserved
        system_msgs = [m for m in compressed if m["role"] == "system"]
        assert len(system_msgs) >= 1
        assert "IMPORTANT SYSTEM RULE" in system_msgs[0]["content"]

    def test_importance_scoring(self):
        """Test that important messages score higher."""
        from core.context_compressor import ContextCompressor

        compressor = ContextCompressor()

        # High importance: decision
        decision_msg = {"role": "assistant", "content": "我决定使用方案A来解决问题。"}
        score_decision = compressor._score_importance(decision_msg, 0, 10)

        # Low importance: acknowledgment
        ack_msg = {"role": "assistant", "content": "好的，收到。"}
        score_ack = compressor._score_importance(ack_msg, 0, 10)

        assert score_decision > score_ack

    def test_summary_generation_zh(self):
        """Test Chinese summary generation."""
        from core.context_compressor import ContextCompressor

        compressor = ContextCompressor()

        messages = [
            {"role": "user", "content": "我想学习Python编程"},
            {"role": "assistant", "content": "Python是一种高级编程语言..."},
            {"role": "user", "content": "能教我写函数吗？"},
            {"role": "assistant", "content": "当然，让我来教你定义函数..."},
        ] * 3

        summary = compressor.generate_summary_replacement(messages, lang="zh")

        assert summary["role"] == "system"
        assert "Python" in summary["content"] or "编程" in summary["content"]

    def test_tiered_compressor(self):
        """Test tier-based compressor selection."""
        from core.context_compressor import TieredCompressor, ContextCompressor

        compressor = TieredCompressor.for_tier("trivial")
        assert isinstance(compressor, ContextCompressor)

        compressor = TieredCompressor.for_tier("expert")
        assert isinstance(compressor, ContextCompressor)


class TestStreaming:
    """Test streaming response functionality."""

    def test_buffer_basic(self):
        """Test streaming buffer."""
        from core.streaming import StreamingBuffer, StreamChunk

        buffer = StreamingBuffer(min_chunk_size=5, max_chunk_size=20)

        # Short text - may or may not flush depending on chunk size
        chunks = buffer.add("Hi")
        assert len(chunks) == 0  # Too short to flush

        chunks = buffer.add("Hello, world!")
        # "Hi" + "Hello, world!" = 14 chars >= min_chunk_size(5), should flush
        assert len(chunks) > 0

    def test_buffer_flush(self):
        """Test buffer flushing."""
        from core.streaming import StreamingBuffer

        buffer = StreamingBuffer(min_chunk_size=50, max_chunk_size=100)

        buffer.add("Short text")
        chunks = buffer.flush()

        assert len(chunks) == 1
        assert chunks[0].content == "Short text"
        assert chunks[0].is_final

    def test_sse_formatter(self):
        """Test SSE formatting."""
        from core.streaming import SSEFormatter, StreamChunk

        formatter = SSEFormatter()

        chunk = StreamChunk(content="Hello", chunk_type="text")
        sse = formatter.format(chunk)

        assert "data:" in sse
        assert "Hello" in sse

    def test_sse_formatter_event(self):
        """Test SSE event formatting."""
        from core.streaming import SSEFormatter

        formatter = SSEFormatter()

        sse = formatter.format_event("complete", {"status": "done"})

        assert "event: complete" in sse
        assert "status" in sse

    def test_streaming_response_stats(self):
        """Test streaming response statistics."""
        from core.streaming import StreamingResponse

        stream = StreamingResponse("test-session")

        stats = stream.get_stats()
        assert stats["session_id"] == "test-session"
        assert stats["is_active"] is True
        assert stats["cancelled"] is False

    def test_streaming_cancel(self):
        """Test cancelling a streaming response."""
        from core.streaming import StreamingResponse

        stream = StreamingResponse("test-session")
        stream.cancel()

        assert stream.cancelled is True
        assert stream.is_active() is False


class TestWebhookTrigger:
    """Test webhook event trigger."""

    def test_webhook_creation(self):
        """Test creating a webhook."""
        from core.webhook_trigger import Webhook, WebhookAuth

        webhook = Webhook(
            name="test-webhook",
            url="https://example.com/webhook",
            auth_type=WebhookAuth.API_KEY.value,
            api_key="test-key",
        )

        assert webhook.name == "test-webhook"
        assert webhook.url == "https://example.com/webhook"
        assert webhook.enabled is True

    def test_register_webhook(self):
        """Test registering a webhook."""
        from core.webhook_trigger import WebhookTrigger, Webhook

        trigger = WebhookTrigger()

        webhook = Webhook(name="test", url="https://example.com")
        trigger.register(webhook)

        retrieved = trigger.get_webhook(webhook.id)
        assert retrieved is not None
        assert retrieved.name == "test"

    def test_unregister_webhook(self):
        """Test unregistering a webhook."""
        from core.webhook_trigger import WebhookTrigger, Webhook

        trigger = WebhookTrigger()

        webhook = Webhook(name="test", url="https://example.com")
        trigger.register(webhook)

        removed = trigger.unregister(webhook.id)
        assert removed is True
        assert trigger.get_webhook(webhook.id) is None

    def test_list_webhooks(self):
        """Test listing webhooks."""
        from core.webhook_trigger import WebhookTrigger, Webhook

        trigger = WebhookTrigger()

        trigger.register(Webhook(name="a", url="http://a.com"))
        trigger.register(Webhook(name="b", url="http://b.com", enabled=False))
        trigger.register(Webhook(name="c", url="http://c.com"))

        all_webhooks = trigger.list_webhooks()
        assert len(all_webhooks) == 3

        enabled = trigger.list_webhooks(enabled_only=True)
        assert len(enabled) == 2

    def test_rate_limiting(self):
        """Test rate limiting."""
        from core.webhook_trigger import WebhookTrigger

        trigger = WebhookTrigger()

        # Should allow up to 10 requests per minute
        for i in range(10):
            assert trigger._check_rate_limit("test-hook", 10) is True

        # 11th should be rate limited
        assert trigger._check_rate_limit("test-hook", 10) is False

    def test_filter_matching(self):
        """Test event filter matching."""
        from core.webhook_trigger import WebhookTrigger

        trigger = WebhookTrigger()

        # Simple key=value filter
        event = {"user": "alice", "action": "login"}
        assert trigger._matches_filter(event, "user=alice") is True
        assert trigger._matches_filter(event, "user=bob") is False

    def test_payload_template(self):
        """Test payload template rendering."""
        from core.webhook_trigger import WebhookTrigger

        trigger = WebhookTrigger()

        template = '{"user": "{{user}}", "action": "{{action}}"}'
        event = {"user": "alice", "action": "login"}

        rendered = trigger._render_payload(template, event)
        assert rendered["user"] == "alice"
        assert rendered["action"] == "login"

    def test_stats(self):
        """Test webhook statistics."""
        from core.webhook_trigger import WebhookTrigger, Webhook

        trigger = WebhookTrigger()

        trigger.register(Webhook(name="test", url="http://test.com"))

        stats = trigger.get_stats()
        assert stats["total_webhooks"] == 1
        assert "webhooks" in stats

    def test_create_slack_webhook(self):
        """Test creating Slack webhook."""
        from core.webhook_trigger import create_slack_webhook

        webhook = create_slack_webhook(
            "https://hooks.slack.com/services/xxx",
            channel="#alerts",
        )

        assert "slack" in webhook.name
        assert webhook.url == "https://hooks.slack.com/services/xxx"
        assert "#alerts" in webhook.payload_template

    def test_create_generic_webhook(self):
        """Test creating generic webhook."""
        from core.webhook_trigger import create_generic_webhook, WebhookAuth

        webhook = create_generic_webhook(
            name="my-webhook",
            url="https://api.example.com/hook",
            event_type="task_completed",
            auth_type=WebhookAuth.BEARER.value,
            api_key="secret",
        )

        assert webhook.name == "my-webhook"
        assert webhook.auth_type == "bearer"
        assert "task_completed" in webhook.event_filter