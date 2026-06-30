"""Mock-based integration tests — verify behavior without requiring a running server."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.mark.asyncio
async def test_health_check_mock():
    """Test health check functionality using mock."""
    from monitor.health import HealthChecker

    health = HealthChecker()

    def mock_db_check():
        return {"status": "healthy", "message": "OK"}

    health.register_check("database", mock_db_check)

    result = health.check_all()
    assert "database" in result["components"]
    assert result["components"]["database"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_tracer_mock():
    """Test distributed tracing using mock."""
    from monitor.tracing import SimpleTracer

    tracer = SimpleTracer("test-service")

    with tracer.start_as_current_span("test-operation") as span:
        span.set_attribute("test.key", "test-value")

    assert len(tracer._spans) >= 1
    assert tracer._spans[-1].name == "test-operation"


@pytest.mark.asyncio
async def test_circuit_breaker_mock():
    """Test circuit breaker behavior using mock."""
    from models import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=0.1)

    # Initial state should allow execution
    assert cb.can_execute() is True

    # Record some failures
    cb.record_failure()
    cb.record_failure()
    assert cb.state == "CLOSED"

    # Third failure should trip the circuit
    cb.record_failure()
    assert cb.state == "OPEN"
    assert cb.can_execute() is False

    # Wait for recovery timeout
    import time
    time.sleep(0.15)
    assert cb.can_execute() is True
    assert cb.state == "HALF_OPEN"


@pytest.mark.asyncio
async def test_semantic_cache_mock():
    """Test semantic cache using mock."""
    from models.semantic_cache import SemanticCache

    cache = SemanticCache()

    # Test cache stats (cache starts empty)
    stats = cache.stats()
    assert "size" in stats
    assert "max_size" in stats


@pytest.mark.asyncio
async def test_failure_recovery_mock():
    """Test failure recovery using mock."""
    from core.failure_recovery import FailureRecovery

    recovery = FailureRecovery()

    # Track retries
    retry_count = 0

    async def failing_operation():
        nonlocal retry_count
        retry_count += 1
        if retry_count < 3:
            raise ValueError("Temporary failure")
        return "success"

    result = await recovery.with_retry(failing_operation)

    assert result == "success"
    assert retry_count == 3


@pytest.mark.asyncio
async def test_style_adapter_mock():
    """Test style adapter using mock."""
    from core.style_adapter import StyleAdapter

    adapter = StyleAdapter()

    # Test setting style
    adapter.set_style({"verbosity": "brief"})

    # Get current style (style is a property)
    style = adapter.style
    assert "verbosity" in style


@pytest.mark.asyncio
async def test_sentiment_analyzer_mock():
    """Test sentiment analyzer using mock."""
    from core.sentiment import SentimentAnalyzer

    analyzer = SentimentAnalyzer()

    result = analyzer.analyze("I love this product! It's amazing!")
    assert "emotion" in result
    assert "confidence" in result


@pytest.mark.asyncio
async def test_knowledge_graph_mock():
    """Test knowledge graph using mock."""
    from memory.knowledge_graph import KnowledgeGraph

    kg = KnowledgeGraph()

    # Add entities
    kg.add_entity("Alice", "person")
    kg.add_entity("Bob", "person")

    # Add relations
    kg.add_relation("Alice", "knows", "Bob")

    # Query neighbors
    neighbors = kg.get_neighbors("Alice")
    assert len(neighbors) >= 1


@pytest.mark.asyncio
async def test_context_compressor_mock():
    """Test context compressor using mock."""
    from core.context_compressor import ContextCompressor

    compressor = ContextCompressor()

    messages = [
        {"role": "system", "content": "You are a helpful assistant"},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]

    result = compressor.compress(messages)
    compressed_messages, summary = result

    assert len(compressed_messages) <= len(messages)
    assert isinstance(summary, str)


@pytest.mark.asyncio
async def test_webhook_trigger_mock():
    """Test webhook trigger using mock."""
    from core.webhook_trigger import WebhookTrigger

    trigger = WebhookTrigger()

    # Mock HTTP client
    with patch('httpx.AsyncClient') as mock_client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)

        result = await trigger.trigger_all(
            event_type="test.event",
            event_data={"key": "value"}
        )

        # Even without registered webhooks, should not raise
        assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_task_scheduler_mock():
    """Test task scheduler using mock."""
    from core.task_scheduler import AsyncTaskScheduler, TaskPriority

    scheduler = AsyncTaskScheduler()

    task_id = await scheduler.schedule_delayed(
        func_name="test_task",
        delay_seconds=0.01,
        args={},
        priority=TaskPriority.NORMAL.value
    )

    await asyncio.sleep(0.05)

    # Stop scheduler properly
    await scheduler.stop()

    assert task_id is not None


@pytest.mark.asyncio
async def test_metacognition_mock():
    """Test metacognition engine using mock."""
    from core.metacognition import get_metacognition_engine

    engine = get_metacognition_engine()

    result = engine.analyze_response(
        response_text="The capital of France is Paris.",
        question_text="What is the capital of France?",
    )

    assert "confidence" in result
    assert "hallucination_risk" in result


@pytest.mark.asyncio
async def test_reasoning_mock():
    """Test progressive reasoner using mock."""
    from core.reasoning import get_step_reasoner

    reasoner = get_step_reasoner()

    # Detect task type
    task_types = reasoner.detect_task_type("What is 2 + 2?")
    assert len(task_types) >= 1

    # Check if should use CoT
    should_cot = reasoner.should_use_cot(0.5, task_types)
    assert isinstance(should_cot, bool)


@pytest.mark.asyncio
async def test_user_profile_mock():
    """Test user profile store using mock."""
    from memory.user_profile import get_profile_store

    store = get_profile_store()

    # Record a skill usage
    store.record_skill_usage("echo", success=True)

    # Get profile summary
    summary = store.get_profile_summary()
    assert summary is not None
    assert "preferences" in summary


@pytest.mark.asyncio
async def test_dialog_summary_mock():
    """Test dialog summarizer using mock."""
    from memory.dialog_summary import get_dialog_summarizer

    summarizer = get_dialog_summarizer()

    # Check if should summarize (new session)
    should_summarize = summarizer.should_summarize("new_session")
    assert isinstance(should_summarize, bool)

    # Get stats
    stats = summarizer.stats()
    assert "active_sessions" in stats


@pytest.mark.asyncio
async def test_backup_export_mock():
    """Test backup export using mock."""
    from core.backup_export import DataExporter

    exporter = DataExporter()

    # Create a minimal backup
    result = exporter.export_all(
        output_path="/tmp/test_backup.zip",
        include_config=False,
    )
    assert result is not None
    assert hasattr(result, 'success')
