import json
import time

import httpx
import pytest

from scribe_plus import engines as E


class Recorder:
    """httpx MockTransport handler that records requests and replies from a table."""

    def __init__(self, routes):
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.host, request.url.path)
        reply = self.routes.get(key)
        if reply is None:
            return httpx.Response(404, json={"error": "no route"})
        if callable(reply):
            return reply(request)
        status, body = reply
        return httpx.Response(status, json=body)


def pool_for(routes):
    rec = Recorder(routes)
    return E.HttpPool(transport=httpx.MockTransport(rec)), rec


def form_fields(req: httpx.Request) -> dict:
    body = req.read().decode("latin-1")
    out = {}
    for part in body.split("--")[1:]:
        if 'name="' not in part:
            continue
        name = part.split('name="', 1)[1].split('"', 1)[0]
        value = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0] if "\r\n\r\n" in part else ""
        out[name] = value
    return out


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk_test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak_test")


def test_groq_transcribe_sends_prompt_language_and_bills_10s(keys):
    pool, rec = pool_for({("POST", "api.groq.com", "/openai/v1/audio/transcriptions"):
                          (200, {"text": " Hello world. "})})
    res = E.transcribe("groq", b"RIFFdata", model="whisper-large-v3-turbo", language="en",
                       prompt="Vocabulary: GrowFlow.", pool=pool, audio_s=2.0)
    assert res.text == "Hello world."
    req = rec.requests[0]
    assert req.headers["authorization"] == "Bearer gsk_test"
    f = form_fields(req)
    assert f["model"] == "whisper-large-v3-turbo" and f["language"] == "en"
    assert f["prompt"] == "Vocabulary: GrowFlow." and f["temperature"] == "0"
    assert f["response_format"] == "json"
    assert abs(res.cost_usd - 10 / 3600 * 0.04) < 1e-12


def test_auto_language_is_omitted(keys):
    pool, rec = pool_for({("POST", "api.openai.com", "/v1/audio/transcriptions"): (200, {"text": "hola"})})
    res = E.transcribe("openai", b"x", model="gpt-4o-mini-transcribe", language="auto",
                       prompt="", pool=pool, audio_s=30.0)
    f = form_fields(rec.requests[0])
    assert "language" not in f and "prompt" not in f and "temperature" not in f
    assert abs(res.cost_usd - 0.5 * 0.003) < 1e-12


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    pool, _ = pool_for({})
    with pytest.raises(E.EngineError, match="no Groq API key"):
        E.transcribe("groq", b"x", model="m", language="en", prompt="", pool=pool, audio_s=1)


def test_http_error_surfaces_status(keys):
    pool, _ = pool_for({("POST", "api.groq.com", "/openai/v1/audio/transcriptions"):
                        (429, {"error": {"message": "rate limit"}})})
    with pytest.raises(E.EngineError, match="HTTP 429"):
        E.transcribe("groq", b"x", model="m", language="en", prompt="", pool=pool, audio_s=1)


def test_dropped_connection_is_retried_once(keys):
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(200, json={"text": "ok"})

    pool, _ = pool_for({("POST", "api.groq.com", "/openai/v1/audio/transcriptions"): flaky})
    res = E.transcribe("groq", b"x", model="m", language="en", prompt="", pool=pool, audio_s=1)
    assert res.text == "ok" and calls["n"] == 2


def test_groq_gpt_oss_chat_payload_and_usage(keys):
    body = {"choices": [{"message": {"content": "Clean text."}}],
            "usage": {"prompt_tokens": 600, "completion_tokens": 40}}
    pool, rec = pool_for({("POST", "api.groq.com", "/openai/v1/chat/completions"): (200, body)})
    res = E.chat("groq", "SYS", [{"role": "user", "content": "hi"}], model="openai/gpt-oss-20b",
                 pool=pool, timeout=5, max_tokens=700)
    payload = json.loads(rec.requests[0].read())
    assert payload["messages"][0] == {"role": "system", "content": "SYS"}
    assert payload["reasoning_effort"] == "low" and payload["include_reasoning"] is False
    assert payload["max_completion_tokens"] == 700 and payload["temperature"] == 0
    assert "max_tokens" not in payload
    assert res.text == "Clean text." and (res.tokens_in, res.tokens_out) == (600, 40)
    assert abs(res.cost_usd - (600 * 0.075 + 40 * 0.30) / 1e6) < 1e-12


def test_anthropic_chat_headers_and_thinking_off(keys):
    body = {"content": [{"type": "text", "text": "Hello."}],
            "usage": {"input_tokens": 500, "output_tokens": 10}}
    pool, rec = pool_for({("POST", "api.anthropic.com", "/v1/messages"): (200, body)})
    res = E.chat("anthropic", "SYS", [{"role": "user", "content": "hi"}], model="claude-haiku-5-5",
                 pool=pool, timeout=5)
    req = rec.requests[0]
    assert req.headers["x-api-key"] == "ak_test" and req.headers["anthropic-version"] == "2023-06-01"
    payload = json.loads(req.read())
    assert payload["thinking"] == {"type": "disabled"} and payload["system"] == "SYS"
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert res.text == "Hello." and res.tokens_in == 500


def test_ollama_chat_needs_no_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    body = {"choices": [{"message": {"content": "Local."}}], "usage": {}}
    pool, rec = pool_for({("POST", "localhost", "/v1/chat/completions"): (200, body)})
    res = E.chat("ollama", "SYS", [], model="llama3.2:3b", pool=pool, timeout=5,
                 ollama_base="http://localhost:11434/v1")
    payload = json.loads(rec.requests[0].read())
    assert "authorization" not in rec.requests[0].headers
    assert payload["max_tokens"] == 1024 and "reasoning_effort" not in payload
    assert res.text == "Local." and res.cost_usd == 0


def test_prewarm_only_when_idle(keys):
    pool, rec = pool_for({("GET", "api.groq.com", "/openai/v1/models"): (200, {"data": []})})
    E.prewarm_for(pool, "groq", "groq")
    E.prewarm_for(pool, "groq", "groq")
    time.sleep(0.2)
    assert len([r for r in rec.requests if r.url.path.endswith("/models")]) == 1


def test_check_setup_flags_missing_models(keys):
    from scribe_plus.settings import SettingsStore
    import tempfile
    from pathlib import Path
    st = SettingsStore(Path(tempfile.mkdtemp()) / "s.json")
    pool, _ = pool_for({("GET", "api.groq.com", "/openai/v1/models"):
                        (200, {"data": [{"id": "whisper-large-v3-turbo"}, {"id": "openai/gpt-oss-20b"}]})})
    lines = E.check_setup(st, pool)
    assert (True, "Groq whisper-large-v3-turbo: available") in lines
    assert (True, "Groq openai/gpt-oss-20b: available") in lines
    assert (False, "Groq openai/gpt-oss-120b: NOT available on your account") in lines


def test_max_tokens_bounds():
    assert E.max_tokens_for("short") == 512
    assert E.max_tokens_for("x" * 100_000) == 4096
