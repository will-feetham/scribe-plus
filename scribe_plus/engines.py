"""
Speech-to-text and cleanup-model providers.

  Speech:  groq (Whisper Large v3 / Turbo), openai (gpt-4o-mini-transcribe
           and friends), local (faster-whisper on the CPU, optional install)
  Cleanup: groq (gpt-oss), anthropic (Claude Haiku), ollama (any
           OpenAI-compatible local server)

Latency matters more than anything here, so every host gets one keep-alive
HTTP client (no TCP+TLS handshake per dictation) and the connection is
warmed up the moment the hotkey goes down, while the user is still talking.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# How to add local Whisper on this OS (shown in messages).
LOCAL_WHISPER_HINT = (
    "run the Scribe Plus installer again with --local-whisper" if sys.platform == "darwin"
    else "run 'Install local Whisper.bat'")

GROQ_BASE = "https://api.groq.com/openai/v1"
OPENAI_BASE = "https://api.openai.com/v1"
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# List prices, used only for the cost column in the latency log. Sources:
# groq.com/pricing (Whisper per audio hour, 10 s minimum per request),
# console.groq.com/docs/models (gpt-oss), platform.claude.com pricing
# (Haiku 5.5 "from" rates), OpenAI per-minute speech estimates. Oct 2026.
GROQ_STT_PER_HOUR = {"whisper-large-v3-turbo": 0.04, "whisper-large-v3": 0.111}
GROQ_MIN_BILLED_S = 10.0
OPENAI_STT_PER_MIN = {
    "gpt-4o-mini-transcribe": 0.003, "gpt-4o-transcribe": 0.006,
    "gpt-transcribe": 0.0045, "whisper-1": 0.006,
}
LLM_PRICES_PER_MTOK = {
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "qwen/qwen3.8-27b": (0.80, 4.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

STT_ENGINES = ("groq", "openai", "local")
CLEANUP_ENGINES = ("groq", "anthropic", "ollama", "off")
KEY_ENV = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY",
           "anthropic": "ANTHROPIC_API_KEY"}


class EngineError(RuntimeError):
    """A provider call failed (no key, HTTP error, network)."""


def api_key(provider: str) -> str:
    env = KEY_ENV.get(provider)
    return (os.environ.get(env) or "").strip() if env else ""


def stt_cost(engine: str, model: str, audio_s: float) -> float:
    if engine == "groq":
        return max(audio_s, GROQ_MIN_BILLED_S) / 3600.0 * GROQ_STT_PER_HOUR.get(model, 0.04)
    if engine == "openai":
        return audio_s / 60.0 * OPENAI_STT_PER_MIN.get(model, 0.006)
    return 0.0


def llm_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    p_in, p_out = LLM_PRICES_PER_MTOK.get(model, (0.0, 0.0))
    return (tokens_in * p_in + tokens_out * p_out) / 1_000_000.0


@dataclass
class STTResult:
    text: str
    ms: float
    engine: str
    model: str
    cost_usd: float


@dataclass
class LLMResult:
    text: str
    ms: float
    engine: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


class HttpPool:
    """One keep-alive client per API host, shared by all threads."""

    WARM_AFTER_IDLE_S = 20.0

    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._clients: dict[str, httpx.Client] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def client(self, base: str) -> httpx.Client:
        with self._lock:
            c = self._clients.get(base)
            if c is None:
                c = httpx.Client(
                    base_url=base,
                    timeout=httpx.Timeout(20.0, connect=5.0),
                    limits=httpx.Limits(max_keepalive_connections=4, keepalive_expiry=90.0),
                    transport=self._transport,
                )
                self._clients[base] = c
            return c

    def mark_used(self, base: str) -> None:
        self._last[base] = time.monotonic()

    def prewarm(self, base: str, path: str, headers: dict) -> None:
        """Open (or refresh) the connection in the background."""
        if time.monotonic() - self._last.get(base, 0.0) < self.WARM_AFTER_IDLE_S:
            return
        self._last[base] = time.monotonic()

        def _go() -> None:
            try:
                self.client(base).get(path, headers=headers, timeout=5.0)
            except Exception as exc:
                log.debug("prewarm %s failed: %s", base, exc)

        threading.Thread(target=_go, daemon=True).start()

    def request(self, base: str, method: str, path: str, **kw) -> httpx.Response:
        """Send with one retry on a dropped connection; raise EngineError."""
        last: Exception | None = None
        for attempt in range(2):
            try:
                r = self.client(base).request(method, path, **kw)
                self.mark_used(base)
                if r.status_code >= 400:
                    detail = r.text[:300].replace("\n", " ")
                    raise EngineError(f"HTTP {r.status_code}: {detail}")
                return r
            except httpx.TimeoutException as exc:
                raise EngineError(f"timed out after {kw.get('timeout', 20)}s") from exc
            except httpx.TransportError as exc:
                # Usually a pooled connection the server already closed.
                last = exc
                log.info("transport error (attempt %d): %s", attempt + 1, exc)
        raise EngineError(f"network error: {last}")

    def close(self) -> None:
        with self._lock:
            for c in self._clients.values():
                try:
                    c.close()
                except Exception:
                    pass
            self._clients.clear()


def prewarm_for(pool: HttpPool, stt_engine: str, cleanup_engine: str) -> None:
    hosts: set[str] = set()
    if stt_engine in ("groq", "openai"):
        hosts.add(stt_engine)
    if cleanup_engine in ("groq", "anthropic"):
        hosts.add(cleanup_engine)
    for h in hosts:
        key = api_key(h)
        if not key:
            continue
        if h == "groq":
            pool.prewarm(GROQ_BASE, "/models", {"Authorization": f"Bearer {key}"})
        elif h == "openai":
            pool.prewarm(OPENAI_BASE, "/models", {"Authorization": f"Bearer {key}"})
        elif h == "anthropic":
            pool.prewarm(ANTHROPIC_BASE, "/models",
                         {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION})


# ---------------------------------------------------------------------------
# Speech to text
# ---------------------------------------------------------------------------


def transcribe(engine: str, wav: bytes, *, model: str, language: str | None,
               prompt: str, pool: HttpPool, audio_s: float,
               timeout: float = 30.0) -> STTResult:
    t0 = time.perf_counter()
    if engine == "local":
        text = LOCAL_WHISPER.transcribe(wav, model=model, language=language, prompt=prompt)
        return STTResult(text, (time.perf_counter() - t0) * 1000.0, engine, model, 0.0)

    if engine not in ("groq", "openai"):
        raise EngineError(f"unknown speech engine {engine!r}")
    key = api_key(engine)
    if not key:
        raise EngineError(f"no {engine.capitalize()} API key (tray menu > API keys)")
    base = GROQ_BASE if engine == "groq" else OPENAI_BASE
    data = {"model": model, "response_format": "json"}
    if engine == "groq":
        data["temperature"] = "0"
    if language and language != "auto":
        data["language"] = language
    if prompt:
        data["prompt"] = prompt
    r = pool.request(
        base, "POST", "/audio/transcriptions",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("audio.wav", wav, "audio/wav")},
        data=data, timeout=timeout,
    )
    try:
        text = str(r.json().get("text") or "").strip()
    except Exception as exc:
        raise EngineError(f"bad transcription response: {exc}") from exc
    ms = (time.perf_counter() - t0) * 1000.0
    return STTResult(text, ms, engine, model, stt_cost(engine, model, audio_s))


class _LocalWhisper:
    """faster-whisper on the CPU. Optional: `pip install faster-whisper`."""

    def __init__(self) -> None:
        self._models: dict[str, object] = {}
        self._lock = threading.Lock()

    @staticmethod
    def available() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None

    def load(self, model: str):
        with self._lock:
            m = self._models.get(model)
            if m is None:
                if not self.available():
                    raise EngineError("local Whisper is not installed "
                                      f"({LOCAL_WHISPER_HINT})")
                from faster_whisper import WhisperModel  # type: ignore
                t0 = time.perf_counter()
                m = WhisperModel(model, device="cpu", compute_type="int8",
                                 cpu_threads=os.cpu_count() or 4)
                log.info("local whisper %s loaded in %.1fs", model, time.perf_counter() - t0)
                self._models[model] = m
            return m

    def preload(self, model: str) -> None:
        threading.Thread(target=lambda: self._safe_load(model), daemon=True).start()

    def _safe_load(self, model: str) -> None:
        try:
            self.load(model)
        except Exception as exc:
            log.warning("local whisper preload failed: %s", exc)

    def transcribe(self, wav: bytes, *, model: str, language: str | None, prompt: str) -> str:
        import numpy as np
        from scribe_plus.audio import wav_to_pcm
        pcm, _sr = wav_to_pcm(wav)
        audio = pcm.astype(np.float32) / 32768.0
        m = self.load(model)
        segments, _info = m.transcribe(  # type: ignore[attr-defined]
            audio,
            language=None if (not language or language == "auto") else language,
            initial_prompt=prompt or None,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()


LOCAL_WHISPER = _LocalWhisper()


# ---------------------------------------------------------------------------
# Cleanup / command models
# ---------------------------------------------------------------------------


def chat(engine: str, system: str, messages: list[dict], *, model: str,
         pool: HttpPool, timeout: float, max_tokens: int = 1024,
         ollama_base: str = "http://localhost:11434/v1") -> LLMResult:
    t0 = time.perf_counter()
    if engine in ("groq", "ollama"):
        if engine == "groq":
            key = api_key("groq")
            if not key:
                raise EngineError("no Groq API key (tray menu > API keys)")
            base, headers = GROQ_BASE, {"Authorization": f"Bearer {key}"}
        else:
            base, headers = ollama_base.rstrip("/"), {}
        payload: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "stream": False,
        }
        if engine == "groq":
            payload["max_completion_tokens"] = max_tokens
            if model.startswith("openai/gpt-oss"):
                # Reasoning tokens count toward latency; "low" keeps them few.
                payload["reasoning_effort"] = "low"
                payload["include_reasoning"] = False
        else:
            payload["max_tokens"] = max_tokens
        r = pool.request(base, "POST", "/chat/completions", headers=headers,
                         json=payload, timeout=timeout)
        try:
            body = r.json()
            text = body["choices"][0]["message"].get("content") or ""
            usage = body.get("usage") or {}
            t_in = int(usage.get("prompt_tokens") or 0)
            t_out = int(usage.get("completion_tokens") or 0)
        except Exception as exc:
            raise EngineError(f"bad chat response: {exc}") from exc

    elif engine == "anthropic":
        key = api_key("anthropic")
        if not key:
            raise EngineError("no Anthropic API key (tray menu > API keys)")
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            # Haiku 5.5 thinks adaptively by default; a cleanup pass needs speed.
            "thinking": {"type": "disabled"},
        }
        r = pool.request(
            ANTHROPIC_BASE, "POST", "/messages",
            headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            json=payload, timeout=timeout,
        )
        try:
            body = r.json()
            text = "".join(b.get("text", "") for b in body.get("content", [])
                           if b.get("type") == "text")
            usage = body.get("usage") or {}
            t_in = int(usage.get("input_tokens") or 0)
            t_out = int(usage.get("output_tokens") or 0)
        except Exception as exc:
            raise EngineError(f"bad Claude response: {exc}") from exc
    else:
        raise EngineError(f"unknown cleanup engine {engine!r}")

    ms = (time.perf_counter() - t0) * 1000.0
    return LLMResult(text.strip(), ms, engine, model, t_in, t_out,
                     llm_cost(model, t_in, t_out))


def max_tokens_for(text: str) -> int:
    """Room for the rewrite plus a little reasoning on gpt-oss."""
    est = int(len(text) / 3.5) + 1
    return max(512, min(4096, 2 * est + 384))


# ---------------------------------------------------------------------------
# Setup check: keys present, and the configured models still served.
# (Providers retire models; Groq dropped its Llama models for free and
# developer accounts on 2026-08-16, which would otherwise fail silently.)
# ---------------------------------------------------------------------------


def list_models(provider: str, pool: HttpPool, ollama_base: str = "") -> set[str]:
    if provider == "ollama":
        r = pool.request(ollama_base.rstrip("/"), "GET", "/models", timeout=5.0)
    else:
        key = api_key(provider)
        if not key:
            raise EngineError("no API key")
        if provider == "anthropic":
            r = pool.request(ANTHROPIC_BASE, "GET", "/models", timeout=8.0,
                             headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
                             params={"limit": 1000})
        else:
            base = GROQ_BASE if provider == "groq" else OPENAI_BASE
            r = pool.request(base, "GET", "/models", timeout=8.0,
                             headers={"Authorization": f"Bearer {key}"})
    return {str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id")}


def check_setup(settings, pool: HttpPool) -> list[tuple[bool, str]]:
    """Human-readable pass/fail lines for the tray's 'Check setup' item."""
    out: list[tuple[bool, str]] = []
    stt_engine = settings.get("stt_engine")
    cleanup_engine = settings.get("cleanup_engine")
    wanted: dict[str, set[str]] = {}
    if stt_engine in ("groq", "openai"):
        wanted.setdefault(stt_engine, set()).add(settings.stt_model(stt_engine))
    elif stt_engine == "local":
        ok = LOCAL_WHISPER.available()
        out.append((ok, "Local Whisper installed" if ok else
                    f"Local Whisper not installed: {LOCAL_WHISPER_HINT}"))
    if cleanup_engine in ("groq", "anthropic", "ollama"):
        wanted.setdefault(cleanup_engine, set()).update(
            {settings.cleanup_model(cleanup_engine), settings.command_model(cleanup_engine)})
    for provider, models in wanted.items():
        name = {"groq": "Groq", "openai": "OpenAI", "anthropic": "Anthropic",
                "ollama": "Ollama"}[provider]
        try:
            available = list_models(provider, pool, settings.get("ollama_base_url") or "")
        except EngineError as exc:
            out.append((False, f"{name}: {exc}"))
            continue
        except Exception as exc:
            out.append((False, f"{name}: {exc}"))
            continue
        for m in sorted(models):
            ok = m in available
            out.append((ok, f"{name} {m}: {'available' if ok else 'NOT available on your account'}"))
    if not out:
        out.append((True, "Nothing to check (cleanup is off)"))
    return out
