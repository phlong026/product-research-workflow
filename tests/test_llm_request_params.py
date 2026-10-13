"""真实 LLM 调用的请求参数:当前模型拒绝非默认采样参数,不能带 temperature 等。"""
import asyncio
from types import SimpleNamespace

import pytest

import research_workflow.cli as cli


def test_llm_complete_sends_no_sampling_params(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    seen = {}

    class FakeMessages:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="[]")])

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    complete = cli._build_llm_complete()
    assert asyncio.run(complete("hi")) == "[]"
    assert not ({"temperature", "top_p", "top_k"} & set(seen))
    assert seen["model"] and seen["max_tokens"] > 0
