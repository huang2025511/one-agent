"""Performance benchmark tests for startup and response times."""
import gc
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# CI 共享 runner 的 CPU 性能波动大（曾出现本机 0.3s / CI 1.18s），
# 绝对耗时阈值在 CI 环境放宽 5 倍，仅保留"数量级回归"防护作用。
CI_SCALE = 5.0 if os.environ.get("CI") else 1.0


class TestStartupPerformance:
    """Test startup performance metrics."""

    def test_import_time(self):
        """Test how fast core modules can be imported."""
        # Clear any cached imports
        modules_to_test = [
            "core.coordinator",
            "core.suggestions",
            "core.sentiment",
            "core.metacognition",
            "core.reasoning",
            "memory.knowledge_graph",
            "models.semantic_cache",
            "monitor.prometheus",
            "monitor.health",
        ]

        for module in modules_to_test:
            if module in sys.modules:
                del sys.modules[module]

        start = time.perf_counter()
        for module in modules_to_test:
            __import__(module)
        elapsed = time.perf_counter() - start

        # All core modules should import in under 2 seconds
        assert elapsed < 2.0 * CI_SCALE, f"Module imports took {elapsed:.2f}s, expected < {2.0 * CI_SCALE:.1f}s"
        print(f"\n  Module imports: {elapsed:.3f}s")

    def test_coordinator_instantiation(self):
        """Test how fast Coordinator can be instantiated."""
        from core.coordinator import Coordinator

        gc.collect()
        start = time.perf_counter()
        Coordinator()
        elapsed = time.perf_counter() - start

        # Coordinator should instantiate in under 1 second
        assert elapsed < 1.0 * CI_SCALE, f"Coordinator instantiation took {elapsed:.2f}s"
        print(f"\n  Coordinator instantiation: {elapsed:.3f}s")

    def test_knowledge_graph_operations(self):
        """Benchmark knowledge graph operations."""
        from memory.knowledge_graph import KnowledgeGraph

        kg = KnowledgeGraph()

        # Add entities
        start = time.perf_counter()
        for i in range(100):
            kg.add_entity(f"entity_{i}", "test")
        add_time = time.perf_counter() - start

        # Add relations
        start = time.perf_counter()
        for i in range(99):
            kg.add_relation(f"entity_{i}", "connects_to", f"entity_{i+1}")
        relation_time = time.perf_counter() - start

        # Query neighbors
        start = time.perf_counter()
        for _ in range(100):
            kg.get_neighbors("entity_50")
        query_time = time.perf_counter() - start

        print(f"\n  KG add 100 entities: {add_time:.3f}s")
        print(f"  KG add 99 relations: {relation_time:.3f}s")
        print(f"  KG 100 neighbor queries: {query_time:.3f}s")

        # Performance assertions
        assert add_time < 1.0 * CI_SCALE, f"Adding entities took {add_time:.2f}s"
        assert relation_time < 1.0 * CI_SCALE, f"Adding relations took {relation_time:.2f}s"
        assert query_time < 1.0 * CI_SCALE, f"100 queries took {query_time:.2f}s"


class TestMemoryPerformance:
    """Test memory-related performance."""

    def test_profile_store_operations(self):
        """Benchmark user profile store operations."""
        from memory.user_profile import UserProfileStore

        store = UserProfileStore()

        start = time.perf_counter()
        for i in range(100):
            store.record_skill_usage(f"skill_{i % 10}", success=i % 10 != 0)
        elapsed = time.perf_counter() - start

        print(f"\n  100 skill usage records: {elapsed:.3f}s")
        assert elapsed < 2.0 * CI_SCALE, f"Recording 100 skills took {elapsed:.2f}s"

    def test_dialog_summarizer_operations(self):
        """Benchmark dialog summarizer operations."""
        from memory.dialog_summary import DialogSummarizer

        summarizer = DialogSummarizer(summary_interval=2)

        # Add messages to trigger summarization
        session_id = "perf_test_session"
        start = time.perf_counter()
        for _i in range(10):
            summarizer.increment_turn(session_id)
        elapsed = time.perf_counter() - start

        print(f"\n  10 turn increments: {elapsed:.3f}s")
        assert elapsed < 0.5 * CI_SCALE, f"10 turns took {elapsed:.2f}s"


class TestIntelligenceModules:
    """Test intelligence module performance."""

    def test_sentiment_analysis_performance(self):
        """Benchmark sentiment analysis."""
        from core.sentiment import SentimentAnalyzer

        analyzer = SentimentAnalyzer()
        texts = [
            "I love this product! It's amazing!",
            "I'm not very happy with this.",
            "This is okay, nothing special.",
            "I'm frustrated with the quality.",
            "Great experience overall!",
        ]

        start = time.perf_counter()
        for _ in range(100):
            for text in texts:
                analyzer.analyze(text)
        elapsed = time.perf_counter() - start

        print(f"\n  500 sentiment analyses: {elapsed:.3f}s")
        assert elapsed < 2.0 * CI_SCALE, f"500 analyses took {elapsed:.2f}s"

    def test_metacognition_performance(self):
        """Benchmark metacognition engine."""
        from core.metacognition import get_metacognition_engine

        engine = get_metacognition_engine()

        start = time.perf_counter()
        for i in range(100):
            engine.analyze_response(
                response_text=f"Response {i}",
                question_text=f"Question {i}",
            )
        elapsed = time.perf_counter() - start

        print(f"\n  100 metacognition analyses: {elapsed:.3f}s")
        assert elapsed < 2.0 * CI_SCALE, f"100 analyses took {elapsed:.2f}s"

    def test_reasoning_performance(self):
        """Benchmark reasoning engine."""
        from core.reasoning import get_step_reasoner

        reasoner = get_step_reasoner()

        start = time.perf_counter()
        for i in range(100):
            reasoner.detect_task_type(f"What is {i} + {i}?")
        elapsed = time.perf_counter() - start

        print(f"\n  100 task type detections: {elapsed:.3f}s")
        assert elapsed < 1.0 * CI_SCALE, f"100 detections took {elapsed:.2f}s"


class TestContextCompression:
    """Test context compression performance."""

    def test_compression_performance(self):
        """Benchmark context compression."""
        from core.context_compressor import ContextCompressor

        compressor = ContextCompressor()

        # Create realistic conversation
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"User message {i}"})
            messages.append({"role": "assistant", "content": f"Assistant response {i}"})

        start = time.perf_counter()
        for _ in range(100):
            compressor.compress(messages, estimated_avg_token_per_char=0.4)
        elapsed = time.perf_counter() - start

        print(f"\n  100 context compressions (50 msg each): {elapsed:.3f}s")
        assert elapsed < 2.0 * CI_SCALE, f"100 compressions took {elapsed:.2f}s"
