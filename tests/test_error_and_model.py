"""Error rendering (402 + truncation), response-budget cap, model-switch
persistence, and identity refresh."""


from quickcode.app import _explain_error
from quickcode.config import Config
from quickcode.core.history import History
from quickcode.prompts.system import render_system_prompt
from quickcode.providers.base import ChatRequest


class _Agent:
    model = "x/model"


def test_402_is_explained_cleanly_without_dumping_json():
    huge = "Error code: 402 - " + '{"message":"requires more credits"} ' * 50
    msg = _explain_error(huge, _Agent())
    assert "402" in msg and "credits" in msg.lower()
    assert "{" not in msg  # no raw JSON
    assert len(msg) < 220


def test_unknown_error_is_truncated():
    msg = _explain_error("boom " * 200, _Agent())
    assert msg.endswith("…")
    assert len(msg) < 200


def test_response_budget_is_capped_by_default():
    # Avoids reserving the model's full output window (402s low-balance accounts).
    assert ChatRequest(model="m", messages=[]).max_tokens == 16384


def test_last_model_persists(tmp_path):
    cfg = Config()
    cfg.last_model = "meta-llama/llama-4-scout"
    path = tmp_path / "config.json"
    cfg.save(path)
    assert Config.load(path).last_model == "meta-llama/llama-4-scout"


def test_set_system_prompt_refreshes_identity():
    from quickcode.config import Environment

    env = Environment.detect()
    hist = History(render_system_prompt(env, model="anthropic/claude-opus-4.8"))
    assert "claude-opus-4.8" in hist._system.content
    hist.set_system_prompt(render_system_prompt(env, model="meta-llama/llama-4-scout"))
    assert "meta-llama/llama-4-scout" in hist._system.content
    assert "claude-opus-4.8" not in hist._system.content
