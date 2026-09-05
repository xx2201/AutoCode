import json

import pytest

from autocode.config import Config
from autocode.context.manager import ContextManager
from autocode.web import runner as runner_module
from autocode.web.model_config import ModelConfigStore, normalize_model_config
from autocode.web.runner import LocalRunner, RunnerSettings


def _runner(tmp_path, *, model_config_path=None):
    settings = RunnerSettings(
        relay_url="https://relay.example",
        token="runner-token-that-is-long-enough",
        ca_cert=str(tmp_path / "unused.pem"),
    )
    return LocalRunner(
        settings,
        config=Config(
            model="initial-model",
            api_key="initial-secret",
            base_url="https://initial.example/v1",
        ),
        model_config_path=model_config_path,
        client=object(),
    )


def test_model_config_store_round_trips_settings_without_affecting_other_config(tmp_path):
    path = tmp_path / "model-config.json"
    store = ModelConfigStore(path)
    original = Config(
        model="initial-model",
        api_key="initial-secret",
        base_url="https://initial.example/v1",
        max_tokens=128,
        max_context_tokens=256_000,
    )
    updated = Config(
        model="updated-model",
        api_key="updated-secret",
        base_url="http://localhost:4000/v1",
        provider="openai",
        max_tokens=128,
    )

    store.save(updated)
    restored = store.apply(original)

    assert restored.model == "updated-model"
    assert restored.api_key == "updated-secret"
    assert restored.base_url == "http://localhost:4000/v1"
    assert restored.provider == "openai"
    assert restored.max_tokens == 128
    assert restored.max_context_tokens == updated.max_context_tokens
    assert json.loads(path.read_text(encoding="utf-8"))["api_key"] == "updated-secret"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "not-a-url"),
        ("provider", "unknown"),
        ("model", " "),
    ],
)
def test_model_config_validation_rejects_invalid_values(field, value):
    values = {
        "model": "model",
        "api_key": "secret",
        "base_url": "https://api.example/v1",
        "provider": "anthropic",
    }
    values[field] = value

    with pytest.raises(ValueError):
        normalize_model_config(**values)


def test_runner_model_config_update_applies_and_returns_only_public_fields(tmp_path):
    config_path = tmp_path / "model-config.json"
    runner = _runner(tmp_path, model_config_path=config_path)
    try:
        result = runner.execute(
            "update_model_config",
            {
                "model": "updated-model",
                "api_key": "updated-secret",
                "base_url": "https://updated.example/v1",
                "provider": "openai",
                "max_context_tokens": 256_000,
            },
        )

        assert runner._base_config.model == "updated-model"
        assert runner._base_config.provider == "openai"
        assert result["model_config"]["api_key_configured"] is True
        assert result["context_window_tokens"] == 256_000
        assert result["model_config"]["max_context_tokens"] == 256_000
        assert result["model_config"]["context_preparation_percent"] == 5
        restored = ModelConfigStore(config_path).apply(Config())
        assert restored.max_context_tokens == 256_000
        assert "updated-secret" not in json.dumps(result)
    finally:
        runner.close()


@pytest.mark.parametrize("window", [64_000, 128_000, 256_000, 512_000, 1_000_000])
def test_window_settings_drive_scaled_context_boundaries(tmp_path, window):
    runner = _runner(tmp_path)
    try:
        runner.execute("update_model_config", {"max_context_tokens": window})
        config = runner._base_config
        context = ContextManager(config.max_context_tokens, config.max_tokens)
        available = window - config.max_tokens
        preparation = available * 5 // 100
        assert context.reminder_tokens == context.fallback_buffer_tokens == preparation
        assert not context.status(available - 2 * preparation - 1)["remind"]
        assert context.status(available - 2 * preparation)["remind"]
        assert context.status(available - preparation)["fallback"]
        assert not context.status(available - 1)["force"]
        assert context.status(available)["force"]
    finally:
        runner.close()


@pytest.mark.parametrize("window", [0, -1, 32_000, 256000.5, True, "bad"])
def test_invalid_window_cannot_replace_saved_runner_config(tmp_path, window):
    runner = _runner(tmp_path, model_config_path=tmp_path / "config.json")
    original = runner._base_config
    try:
        with pytest.raises(ValueError):
            runner.execute("update_model_config", {"max_context_tokens": window})
        assert runner._base_config is original
        assert not (tmp_path / "config.json").exists()
    finally:
        runner.close()


def test_runner_model_config_connection_test_uses_selected_adapter_and_redacts_key(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    calls = []

    class FakeResponse:
        content = "OK"

    class FakeLLM:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def chat(self, messages):
            assert messages == [{"role": "user", "content": "Reply with OK only."}]
            return FakeResponse()

    monkeypatch.setattr(runner_module, "llm_class_for_provider", lambda _: FakeLLM)
    try:
        result = runner.execute(
            "test_model_config",
            {
                "model": "test-model",
                "api_key": "test-secret",
                "base_url": "https://test.example/v1",
                "provider": "openai",
            },
        )
        assert result["ok"] is True
        assert result["response"] == "OK"
        assert calls[0]["model"] == "test-model"
        assert calls[0]["api_key"] == "test-secret"

        class FailingLLM(FakeLLM):
            def chat(self, messages):
                raise RuntimeError("provider rejected test-secret")

        monkeypatch.setattr(runner_module, "llm_class_for_provider", lambda _: FailingLLM)
        with pytest.raises(ValueError, match=r"\[redacted\]") as error:
            runner.execute(
                "test_model_config",
                {
                    "model": "test-model",
                    "api_key": "test-secret",
                    "base_url": "https://test.example/v1",
                    "provider": "openai",
                },
            )
        assert "test-secret" not in str(error.value)
    finally:
        runner.close()
