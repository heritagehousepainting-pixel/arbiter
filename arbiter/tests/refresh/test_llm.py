from arbiter.refresh.llm import LLM, FakeLLM


def test_fakellm_mirrors_sdk_shape():
    fake = FakeLLM("```json\n{\"market\": []}\n```")
    resp = fake.create(model="m", max_tokens=10, thinking={"type": "adaptive"},
                       tools=[], messages=[{"role": "user", "content": "hi"}])
    assert resp.stop_reason == "end_turn"
    text = "".join(b.text for b in resp.content if b.type == "text")
    assert "market" in text


def test_fakellm_satisfies_protocol():
    assert isinstance(FakeLLM("x"), LLM)
