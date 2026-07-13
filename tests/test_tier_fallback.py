"""V62 4 层路由故障切换测试：同层切换 + 跨层降级。

验证当某层某个模型/服务商调用失败时，会自动切换到：
1. 同层其他有可用 key 的模型（不降级）
2. 相邻下层模型（降级）
3. 跳过已标记 key 失效的 provider
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.tiers import MODEL_TIERS


class MockProvider:
    """模拟 LLMProvider 用于测试 tier fallback 候选生成逻辑"""

    def __init__(self, keys=None, invalid_keys=None):
        self._keys = keys or {}
        self._invalid_keys = set(invalid_keys or [])

    def _has_usable_key(self, provider):
        return provider in self._keys and provider not in self._invalid_keys

    def _get_tier_fallback_candidates(self, current_model):
        """复制 models/__init__.py 的逻辑用于测试"""
        current_tier = None
        for tier_name, models in MODEL_TIERS.items():
            if current_model in models:
                current_tier = tier_name
                break
        if current_tier is None:
            return []
        tier_order = ["expert", "complex", "simple", "trivial"]
        try:
            current_idx = tier_order.index(current_tier)
        except ValueError:
            current_idx = 0
        candidates = []
        seen = {current_model}
        # 同层
        for model in MODEL_TIERS.get(current_tier, []):
            if model in seen:
                continue
            provider = model.split("/", 1)[0] if "/" in model else ""
            if not provider or provider in self._invalid_keys:
                continue
            if not self._has_usable_key(provider):
                continue
            seen.add(model)
            candidates.append(model)
        # 下层
        for offset in range(1, len(tier_order) - current_idx):
            lower_idx = current_idx + offset
            if lower_idx >= len(tier_order):
                break
            lower_tier = tier_order[lower_idx]
            for model in MODEL_TIERS.get(lower_tier, []):
                if model in seen:
                    continue
                provider = model.split("/", 1)[0] if "/" in model else ""
                if not provider or provider in self._invalid_keys:
                    continue
                if not self._has_usable_key(provider):
                    continue
                seen.add(model)
                candidates.append(model)
        return candidates


def test_same_tier_switch():
    """测试1：同层模型切换 — expert 层模型失败，切到同层其他模型"""
    print("\n=== 测试1：同层模型切换 ===")
    p = MockProvider(keys={"anthropic": "k1", "openai": "k2", "sensenova": "k3"})

    # expert 层第一个模型 anthropic/claude-4.5-sonnet 失败
    current = "anthropic/claude-4.5-sonnet-20250514"
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (expert)")
    print(f"  候选: {cands}")

    # 应该包含 expert 同层的 openai/o3
    assert "openai/o3" in cands, f"应该切到同层 openai/o3, got {cands}"
    print("  ✅ 同层切换到 openai/o3")


def test_cross_tier_downgrade():
    """测试2：跨层降级 — expert 层全挂，降级到 complex 层"""
    print("\n=== 测试2：跨层降级 ===")
    # 只有 sensenova key，expert 层 kimi 无 key
    p = MockProvider(keys={"sensenova": "k1"})

    current = "kimi/kimi-k2-0711-preview"  # expert 层，但 kimi 无 key
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (expert, 无key)")
    print(f"  候选: {cands}")

    # expert 同层无其他有 key 的模型，应该降级到 complex 层 sensenova/deepseek-v4-flash
    assert "sensenova/deepseek-v4-flash" in cands, f"应该降级到 complex 层 deepseek-v4-flash, got {cands}"
    print("  ✅ 跨层降级到 sensenova/deepseek-v4-flash (complex)")


def test_multi_tier_downgrade():
    """测试3：多层降级 — expert 失败，一路降级到 trivial"""
    print("\n=== 测试3：多层降级 ===")
    # 只有 sensenova key，且 complex 层的 deepseek-v4 也挂了
    p = MockProvider(keys={"sensenova": "k1"})

    current = "sensenova/deepseek-v4-flash"  # complex 层
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (complex)")
    print(f"  候选: {cands}")

    # 同层无其他 sensenova 模型，降级到 simple/trivial 的 sensenova
    assert "sensenova/sensenova-6.7-flash-lite" in cands, f"应该降级到 sensenova-6.7-flash-lite, got {cands}"
    print("  ✅ 多层降级到 sensenova/sensenova-6.7-flash-lite (simple→trivial)")


def test_skip_invalid_provider():
    """测试4：跳过已标记 key 失效的 provider"""
    print("\n=== 测试4：跳过失效 provider ===")
    # anthropic key 失效
    p = MockProvider(keys={"anthropic": "k1", "sensenova": "k2"}, invalid_keys=["anthropic"])

    current = "anthropic/claude-4.5-sonnet-20250514"  # expert 层
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (anthropic 已失效)")
    print(f"  候选: {cands}")

    # 应该跳过所有 anthropic 模型，只保留 sensenova
    assert all("anthropic" not in c for c in cands), f"不应包含 anthropic 模型, got {cands}"
    assert "sensenova/deepseek-v4-flash" in cands, f"应该包含 sensenova 模型, got {cands}"
    print("  ✅ 跳过失效的 anthropic，切到 sensenova")


def test_no_fallback_available():
    """测试5：无可用候选 — 所有 provider 都无 key"""
    print("\n=== 测试5：无可用候选 ===")
    p = MockProvider(keys={})  # 没有任何 key

    current = "anthropic/claude-4.5-sonnet-20250514"
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (无任何 key)")
    print(f"  候选: {cands}")

    assert len(cands) == 0, f"应该无候选, got {cands}"
    print("  ✅ 无候选（所有 provider 无 key）")


def test_custom_model_not_in_tier():
    """测试6：自定义模型不在任何 tier 中"""
    print("\n=== 测试6：自定义模型 ===")
    p = MockProvider(keys={"custom": "k1"})

    current = "custom/my-model"  # 不在任何 tier 中
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (不在任何 tier)")
    print(f"  候选: {cands}")

    assert len(cands) == 0, f"应该无候选（自定义模型）, got {cands}"
    print("  ✅ 自定义模型返回空候选（交由 fallback_chain 处理）")


def test_all_providers_available():
    """测试7：所有 provider 都有 key 时的完整候选列表"""
    print("\n=== 测试7：所有 provider 都有 key ===")
    p = MockProvider(keys={
        "anthropic": "k1", "openai": "k2", "google": "k3",
        "sensenova": "k4", "deepseek": "k5", "qwen": "k6",
        "openrouter": "k7", "kimi": "k8",
    })

    current = "anthropic/claude-4.5-sonnet-20250514"  # expert 层
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (expert)")
    print(f"  候选 ({len(cands)}): {cands}")

    # 应该有大量候选：expert 同层 3 个 + complex 4 个 + simple 3 个 + trivial 4 个
    assert len(cands) >= 10, f"应该有 10+ 候选, got {len(cands)}"
    print(f"  ✅ {len(cands)} 个候选（同层 + 跨层降级）")


def test_complex_to_simple_downgrade():
    """测试8：complex 层失败降级到 simple 层"""
    print("\n=== 测试8：complex → simple 降级 ===")
    # 只有 anthropic key，complex 层的 claude-3.5-sonnet 失败
    p = MockProvider(keys={"anthropic": "k1"})

    current = "anthropic/claude-3.5-sonnet-20241022"  # complex 层
    cands = p._get_tier_fallback_candidates(current)
    print(f"  当前: {current} (complex)")
    print(f"  候选: {cands}")

    # 同层无其他 anthropic，降级到 simple 层 anthropic/claude-3.5-haiku
    assert "anthropic/claude-3.5-haiku-20241022" in cands, f"应该降级到 simple 层 haiku, got {cands}"
    # 还应该有 trivial 层的 anthropic/claude-haiku-latest
    assert "anthropic/claude-haiku-latest" in cands, f"应该有 trivial 层 haiku-latest, got {cands}"
    print("  ✅ complex → simple → trivial 逐层降级")


if __name__ == "__main__":
    test_same_tier_switch()
    test_cross_tier_downgrade()
    test_multi_tier_downgrade()
    test_skip_invalid_provider()
    test_no_fallback_available()
    test_custom_model_not_in_tier()
    test_all_providers_available()
    test_complex_to_simple_downgrade()
    print("\n" + "=" * 60)
    print("🎉 所有 4 层路由故障切换测试通过！")
    print("=" * 60)
