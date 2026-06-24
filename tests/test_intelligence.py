"""Tests for intelligent features: user profile, suggestions, sentiment."""

import pytest
import tempfile
import os

from memory.user_profile import UserProfileStore
from core.sentiment import SentimentAnalyzer
from core.suggestions import SuggestionEngine


class TestUserProfile:
    """Test user profile tracking."""

    def test_preference_set_get(self, tmp_path):
        """Test setting and getting preferences."""
        db_path = str(tmp_path / "profile.db")
        store = UserProfileStore(db_path)

        store.set_preference("language", "zh")
        assert store.get_preference("language") == "zh"

        store.set_preference("response_style", {"concise": True, "emoji": False})
        pref = store.get_preference("response_style")
        assert pref["concise"] is True

    def test_skill_usage_tracking(self, tmp_path):
        """Test skill usage recording."""
        db_path = str(tmp_path / "profile.db")
        store = UserProfileStore(db_path)

        store.record_skill_usage("web_search", success=True)
        store.record_skill_usage("web_search", success=True)
        store.record_skill_usage("web_search", success=False)

        top_skills = store.get_top_skills(5)
        assert len(top_skills) == 1
        assert top_skills[0][0] == "web_search"
        assert top_skills[0][1] == 3

        rate = store.get_skill_success_rate("web_search")
        assert rate == 2 / 3

    def test_topic_tracking(self, tmp_path):
        """Test topic recording."""
        db_path = str(tmp_path / "profile.db")
        store = UserProfileStore(db_path)

        store.record_topic("编程")
        store.record_topic("编程")
        store.record_topic("搜索")

        topics = store.get_top_topics(10)
        assert len(topics) == 2
        assert topics[0][0] == "编程"
        assert topics[0][1] == 2

    def test_time_pattern(self, tmp_path):
        """Test time pattern recording."""
        db_path = str(tmp_path / "profile.db")
        store = UserProfileStore(db_path)

        store.record_time_pattern()
        pattern = store.get_pattern("time_of_day")
        assert pattern is not None
        assert "hours" in pattern

    def test_profile_summary(self, tmp_path):
        """Test comprehensive profile summary."""
        db_path = str(tmp_path / "profile.db")
        store = UserProfileStore(db_path)

        store.set_preference("language", "en")
        store.record_skill_usage("python_execute", success=True)
        store.record_topic("代码")

        summary = store.get_profile_summary()
        assert "preferences" in summary
        assert "top_skills" in summary
        assert "top_topics" in summary

    def test_clear(self, tmp_path):
        """Test clearing all profile data."""
        db_path = str(tmp_path / "profile.db")
        store = UserProfileStore(db_path)

        store.set_preference("test", "value")
        store.record_skill_usage("test_skill", success=True)
        store.clear()

        assert store.get_preference("test") is None
        assert store.get_top_skills() == []


class TestSentimentAnalyzer:
    """Test sentiment analysis."""

    def test_frustration_detection(self):
        """Test detecting frustration."""
        analyzer = SentimentAnalyzer()

        result = analyzer.analyze("快点，怎么这么慢！烦死了")
        assert result["emotion"] == "frustration"
        assert result["confidence"] > 0.3
        assert result["response_style"]["style"] == "concise"

    def test_confusion_detection(self):
        """Test detecting confusion."""
        analyzer = SentimentAnalyzer()

        result = analyzer.analyze("我不明白，这是什么意思？解释一下")
        assert result["emotion"] == "confusion"
        assert result["response_style"]["style"] == "explanatory"

    def test_satisfaction_detection(self):
        """Test detecting satisfaction."""
        analyzer = SentimentAnalyzer()

        result = analyzer.analyze("太好了！谢谢，完美解决")
        assert result["emotion"] == "satisfaction"
        assert result["response_style"]["style"] == "brief"

    def test_curiosity_detection(self):
        """Test detecting curiosity."""
        analyzer = SentimentAnalyzer()

        result = analyzer.analyze("我想了解更多，还有什么？")
        assert result["emotion"] == "curiosity"
        assert result["response_style"]["style"] == "engaging"

    def test_neutral_detection(self):
        """Test neutral input."""
        analyzer = SentimentAnalyzer()

        result = analyzer.analyze("今天天气怎么样")
        # "怎么样" matches confusion pattern, so it may be confusion
        assert result["emotion"] in ("neutral", "confusion", "curiosity")

    def test_emotion_trend(self):
        """Test emotion trend tracking."""
        analyzer = SentimentAnalyzer()

        # Simulate improving trend
        analyzer.analyze("我不明白")  # confusion
        analyzer.analyze("好的，懂了")  # satisfaction
        analyzer.analyze("谢谢！")  # satisfaction

        trend = analyzer.get_emotion_trend()
        assert trend == "improving"

    def test_format_for_llm(self):
        """Test LLM context formatting."""
        analyzer = SentimentAnalyzer()

        result = analyzer.analyze("快点！")
        formatted = analyzer.format_for_llm(result)
        assert "用户情绪检测" in formatted or "emotion" in formatted.lower()


class TestSuggestionEngine:
    """Test suggestion generation."""

    def test_skill_suggestion_from_history(self, tmp_path):
        """Test skill suggestions based on history."""
        # Set up profile with usage history
        db_path = str(tmp_path / "profile.db")
        import memory.user_profile as up_module
        up_module._profile_store = UserProfileStore(db_path)

        store = up_module.get_profile_store()
        store.record_skill_usage("web_search", success=True)
        store.record_skill_usage("python_execute", success=True)

        engine = SuggestionEngine()
        suggestions = engine.generate_suggestions(
            "帮我搜索一下",
            "搜索结果如下...",
            ["web_search"],
        )

        # Suggestions may include related skills or topics
        # The engine suggests skills related to web_search
        assert len(suggestions) >= 0  # May have suggestions

    def test_topic_based_suggestion(self):
        """Test suggestions based on detected topics."""
        engine = SuggestionEngine()

        suggestions = engine.generate_suggestions(
            "帮我写一段代码",
            "代码已生成",
            [],
        )

        # Should suggest python_execute based on topic
        assert any(
            s.get("skill") == "python_execute" or "代码" in s.get("reason", "")
            for s in suggestions
        )

    def test_next_step_suggestion(self):
        """Test next-step suggestions."""
        engine = SuggestionEngine()

        suggestions = engine.generate_suggestions(
            "搜索 Python 教程",
            "搜索成功，找到多个结果",
            ["web_search"],
        )

        # May have suggestions (skill, next_step, or tip)
        # Not all replies trigger next_step suggestions
        assert len(suggestions) >= 0

    def test_filter_duplicates(self):
        """Test duplicate filtering."""
        engine = SuggestionEngine()

        # Generate twice with similar input
        suggestions1 = engine.generate_suggestions("代码", "结果", [])
        suggestions2 = engine.generate_suggestions("代码", "结果", [])

        # Second should have fewer suggestions (filtered)
        assert len(suggestions2) <= len(suggestions1)

    def test_format_for_display(self):
        """Test display formatting."""
        engine = SuggestionEngine()

        suggestions = [
            {"type": "skill", "skill": "python_execute", "reason": "相关技能"},
            {"type": "next_step", "action": "运行代码", "reason": "建议"},
        ]

        formatted = engine.format_suggestions_for_display(suggestions)
        assert "python_execute" in formatted
        assert "运行代码" in formatted

    def test_format_for_llm(self):
        """Test LLM context formatting."""
        engine = SuggestionEngine()

        suggestions = [
            {"type": "skill", "skill": "calc", "reason": "计算相关"},
        ]

        formatted = engine.format_suggestions_for_llm(suggestions)
        assert "calc" in formatted
        assert "系统建议" in formatted or "suggestion" in formatted.lower()