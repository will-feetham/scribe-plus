#!/usr/bin/env python3
"""
Scribe Plus benchmark: replay your own saved dictations through every
speech and cleanup engine you have access to, and compare speed, cost and
agreement. Also summarizes the live latency log from real use.

    venv\\Scripts\\python.exe benchmark.py              # newest 12 clips
    venv\\Scripts\\python.exe benchmark.py --clips 30
    venv\\Scripts\\python.exe benchmark.py --stt groq-turbo,local --cleanup groq-20b,haiku

Results: %APPDATA%\\scribe\\plus\\benchmarks\\benchmark-<time>.csv and .html
"""
from __future__ import annotations

import argparse
import csv
import html
import os
import statistics
import sys
import time
from datetime import datetime

from scribe_plus import audio, engines, textproc
from scribe_plus import settings as S

STT_OPTIONS = {
    "groq-turbo": ("groq", "whisper-large-v3-turbo"),
    "groq-large": ("groq", "whisper-large-v3"),
    "openai-mini": ("openai", "gpt-4o-mini-transcribe"),
    "local": ("local", None),
}
CLEANUP_OPTIONS = {
    "groq-20b": ("groq", "openai/gpt-oss-20b"),
    "groq-120b": ("groq", "openai/gpt-oss-120b"),
    "haiku": ("anthropic", "claude-haiku-5-5"),
    "ollama": ("ollama", None),
}
# Spacing between calls to stay under free-tier limits
# (Groq free: Whisper 20 requests/min; gpt-oss 30 requests/min, 8K tokens/min).
MIN_INTERVAL_S = {("groq", "stt"): 3.2, ("groq", "chat"): 6.0}


class Pacer:
    def __init__(self) -> None:
        self._last: dict = {}

    def wait(self, key: tuple) -> None:
        gap = MIN_INTERVAL_S.get(key[:2], 0.0)
        last = self._last.get(key, 0.0)
        delay = gap - (time.monotonic() - last)
        if delay > 0:
            time.sleep(delay)
        self._last[key] = time.monotonic()


def with_retry(fn, label: str):
    for attempt in range(4):
        try:
            return fn()
        except engines.EngineError as exc:
            if "HTTP 429" in str(exc) and attempt < 3:
                wait = 12 * (attempt + 1)
                print(f"    rate limited on {label}; waiting {wait}s")
                time.sleep(wait)
                continue
            raise


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    v = sorted(values)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def available_stt(settings) -> dict:
    out = {}
    for name, (engine, model) in STT_OPTIONS.items():
        if engine == "local":
            if engines.LOCAL_WHISPER.available():
                out[name] = (engine, settings.stt_model("local"))
        elif engines.api_key(engine):
            out[name] = (engine, model)
    return out


def available_cleanup(settings, pool) -> dict:
    out = {}
    for name, (engine, model) in CLEANUP_OPTIONS.items():
        if engine == "ollama":
            try:
                engines.list_models("ollama", pool, settings.get("ollama_base_url"))
                out[name] = (engine, settings.cleanup_model("ollama"))
            except Exception:
                pass
        elif engines.api_key(engine):
            out[name] = (engine, model)
    return out


def clip_styles() -> dict:
    """clip filename -> style recorded at dictation time, from the live log."""
    styles = {}
    if S.METRICS_FILE.exists():
        with S.METRICS_FILE.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("clip"):
                    styles[row["clip"]] = row.get("style") or "default"
    return styles


def live_summary() -> list[dict]:
    """Median latencies from real dictations, grouped by engine pair."""
    if not S.METRICS_FILE.exists():
        return []
    groups: dict = {}
    with S.METRICS_FILE.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("error") or not row.get("final_text"):
                continue
            key = (row.get("mode", ""), row.get("stt_model", ""), row.get("cleanup_model", "") or "off")
            groups.setdefault(key, []).append(row)
    out = []
    for (mode, stt_m, clean_m), rows in sorted(groups.items()):
        def col(name):
            vals = []
            for r in rows:
                try:
                    vals.append(float(r.get(name) or "nan"))
                except ValueError:
                    pass
            return [v for v in vals if v == v]
        out.append({
            "mode": mode, "stt_model": stt_m, "cleanup_model": clean_m, "n": len(rows),
            "total_p50": pct(col("total_ms"), 0.5), "total_p90": pct(col("total_ms"), 0.9),
            "stt_p50": pct(col("stt_ms"), 0.5), "cleanup_p50": pct(col("cleanup_ms"), 0.5),
            "paste_p50": pct(col("paste_ms"), 0.5),
            "cost_mean": statistics.fmean(col("est_cost_usd")) if col("est_cost_usd") else 0.0,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clips", type=int, default=12, help="newest N saved clips (default 12)")
    ap.add_argument("--stt", default="", help=f"comma list from {', '.join(STT_OPTIONS)}")
    ap.add_argument("--cleanup", default="", help=f"comma list from {', '.join(CLEANUP_OPTIONS)}")
    ap.add_argument("--no-open", action="store_true", help="don't open the HTML report")
    args = ap.parse_args()

    S.load_env_file()
    settings = S.SettingsStore()
    pool = engines.HttpPool()
    pacer = Pacer()
    dictionary = textproc.parse_dictionary(
        S.DICTIONARY_FILE.read_text(encoding="utf-8") if S.DICTIONARY_FILE.exists() else "")
    prompt = textproc.whisper_prompt(dictionary)

    stt = available_stt(settings)
    if args.stt:
        stt = {k: v for k, v in stt.items() if k in args.stt.split(",")}
    cleanup = available_cleanup(settings, pool)
    if args.cleanup:
        cleanup = {k: v for k, v in cleanup.items() if k in args.cleanup.split(",")}

    clips = sorted(S.CLIPS_DIR.glob("*.wav"))[-args.clips:] if S.CLIPS_DIR.exists() else []
    print(f"Scribe Plus benchmark: {len(clips)} clips, speech engines: "
          f"{', '.join(stt) or 'none'}, cleanup engines: {', '.join(cleanup) or 'none'}")
    if not clips:
        print("No saved clips yet. Dictate a few times with 'Save audio for benchmarks' on.")
    if not stt:
        print("No speech engine available: add a Groq or OpenAI key, or install local Whisper.")

    styles = clip_styles()
    rows: list[dict] = []
    warmed: set = set()
    language = settings.get("language")

    for i, clip in enumerate(clips, 1):
        prep = audio.prepare(clip.read_bytes(), boost=bool(settings.get("quiet_voice_boost")),
                             trim=bool(settings.get("trim_silence")))
        if prep.silent:
            print(f"[{i}/{len(clips)}] {clip.name}: silent, skipped")
            continue
        print(f"[{i}/{len(clips)}] {clip.name}  ({prep.kept_s:.1f}s of speech)")
        texts: dict = {}
        for name, (engine, model) in stt.items():
            if name not in warmed:
                # First call pays connection setup; live use warms it while you talk.
                try:
                    pacer.wait((engine, "stt", model))
                    with_retry(lambda: engines.transcribe(engine, prep.wav, model=model, language=language,
                                                          prompt=prompt, pool=pool, audio_s=prep.kept_s), name)
                except Exception:
                    pass
                warmed.add(name)
            pacer.wait((engine, "stt", model))
            try:
                res = with_retry(lambda: engines.transcribe(
                    engine, prep.wav, model=model, language=language, prompt=prompt,
                    pool=pool, audio_s=prep.kept_s), name)
                texts[name] = res.text
                rows.append({"kind": "speech", "clip": clip.name, "engine": name, "model": model,
                             "ms": round(res.ms), "cost_usd": res.cost_usd, "text": res.text})
                print(f"    {name:<12} {res.ms:7.0f} ms  {res.text[:70]!r}")
            except Exception as exc:
                rows.append({"kind": "speech", "clip": clip.name, "engine": name, "model": model,
                             "error": str(exc)[:200]})
                print(f"    {name:<12} failed: {exc}")
        reference = texts.get("groq-large") or texts.get("groq-turbo") or next(iter(texts.values()), "")
        for r in rows:
            if r["clip"] == clip.name and r["kind"] == "speech" and "text" in r:
                r["wer_vs_ref"] = round(textproc.wer(reference, r["text"]), 3)
        source = texts.get("groq-turbo") or reference
        if not source:
            continue
        style = styles.get(clip.name, "default")
        system, messages = textproc.build_cleanup_messages(
            source, style_key=style, style_text=settings.style_text(style), dictionary=dictionary)
        for name, (engine, model) in cleanup.items():
            pacer.wait((engine, "chat", model))
            try:
                res = with_retry(lambda: engines.chat(
                    engine, system, messages, model=model, pool=pool,
                    timeout=float(settings.get("cleanup_timeout_s") or 6.0) * 3,
                    max_tokens=engines.max_tokens_for(source),
                    ollama_base=settings.get("ollama_base_url")), name)
                out = textproc.postprocess_output(res.text, source)
                reason = textproc.guard(source, out)
                rows.append({"kind": "cleanup", "clip": clip.name, "engine": name, "model": model,
                             "ms": round(res.ms), "cost_usd": res.cost_usd, "tokens_in": res.tokens_in,
                             "tokens_out": res.tokens_out, "guard": reason, "style": style,
                             "input": source, "text": out})
                print(f"    {name:<12} {res.ms:7.0f} ms  {out[:70]!r}{'  [guard: ' + reason + ']' if reason else ''}")
            except Exception as exc:
                rows.append({"kind": "cleanup", "clip": clip.name, "engine": name, "model": model,
                             "error": str(exc)[:200]})
                print(f"    {name:<12} failed: {exc}")

    S.BENCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = S.BENCH_DIR / f"benchmark-{stamp}.csv"
    cols = ["kind", "clip", "engine", "model", "ms", "cost_usd", "wer_vs_ref", "tokens_in",
            "tokens_out", "guard", "style", "error", "input", "text"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    summary = summarize(rows)
    live = live_summary()
    print("\nSummary (median and 90th percentile latency, warm connections):")
    for s in summary:
        extra = (f"  disagreement vs reference {s['wer']:.1%}" if s["kind"] == "speech"
                 else f"  guard rejections {s['guard']}")
        print(f"  {s['kind']:<8} {s['engine']:<12} p50 {s['p50']:6.0f} ms  p90 {s['p90']:6.0f} ms  "
              f"~${s['cost'] * 1000:.3f} per 1,000{extra}")
    html_path = S.BENCH_DIR / f"benchmark-{stamp}.html"
    html_path.write_text(render_html(summary, live, rows, len(clips)), encoding="utf-8")
    print(f"\nCSV:    {csv_path}\nReport: {html_path}")
    if not args.no_open:
        try:
            os.startfile(str(html_path))  # type: ignore[attr-defined]
        except Exception:
            pass
    pool.close()
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nPress Enter to close.")
        except EOFError:
            pass
    return 0


def summarize(rows: list[dict]) -> list[dict]:
    out = []
    keys = []
    for r in rows:
        k = (r["kind"], r["engine"], r["model"])
        if k not in keys:
            keys.append(k)
    for kind, engine, model in keys:
        rs = [r for r in rows if (r["kind"], r["engine"], r["model"]) == (kind, engine, model)]
        ok = [r for r in rs if "ms" in r]
        ms = [float(r["ms"]) for r in ok]
        out.append({
            "kind": kind, "engine": engine, "model": model, "n": len(ok), "errors": len(rs) - len(ok),
            "p50": pct(ms, 0.5), "p90": pct(ms, 0.9),
            "cost": statistics.fmean([float(r.get("cost_usd") or 0) for r in ok]) if ok else 0.0,
            "wer": statistics.fmean([float(r["wer_vs_ref"]) for r in ok if "wer_vs_ref" in r])
            if any("wer_vs_ref" in r for r in ok) else 0.0,
            "guard": sum(1 for r in ok if r.get("guard")),
        })
    return out


def render_html(summary: list[dict], live: list[dict], rows: list[dict], n_clips: int) -> str:
    e = html.escape
    max_p90 = max([s["p90"] for s in summary if s["p90"] == s["p90"]] + [1.0])

    def bar(s):
        p50 = s["p50"] if s["p50"] == s["p50"] else 0
        p90 = s["p90"] if s["p90"] == s["p90"] else 0
        return (f'<div class="bar"><span class="p90" style="width:{100 * p90 / max_p90:.1f}%"></span>'
                f'<span class="p50" style="width:{100 * p50 / max_p90:.1f}%"></span></div>')

    parts = []
    for s in summary:
        quality = "{:.1%}".format(s["wer"]) if s["kind"] == "speech" else str(s["guard"])
        runs = str(s["n"]) + (" ({} failed)".format(s["errors"]) if s["errors"] else "")
        parts.append(
            "<tr><td>{}</td><td>{}<div class='m'>{}</div></td><td class='n'>{:.0f}</td>"
            "<td class='n'>{:.0f}</td><td>{}</td><td class='n'>${:.3f}</td><td class='n'>{}</td>"
            "<td class='n'>{}</td></tr>".format(
                e(s["kind"]), e(s["engine"]), e(str(s["model"])), s["p50"], s["p90"], bar(s),
                s["cost"] * 1000, quality, runs))
    srows = "".join(parts)
    lrows = "".join(
        f"<tr><td>{e(r['mode'])}</td><td>{e(r['stt_model'])}</td><td>{e(r['cleanup_model'])}</td>"
        f"<td class='n'>{r['n']}</td><td class='n'>{r['total_p50']:.0f}</td><td class='n'>{r['total_p90']:.0f}</td>"
        f"<td class='n'>{r['stt_p50']:.0f}</td><td class='n'>{r['cleanup_p50']:.0f}</td>"
        f"<td class='n'>{r['paste_p50']:.0f}</td><td class='n'>${r['cost_mean'] * 1000:.3f}</td></tr>"
        for r in live)
    detail = "".join(
        f"<tr><td>{e(r['clip'])}</td><td>{e(r['kind'])}</td><td>{e(r['engine'])}</td>"
        f"<td class='n'>{e(str(r.get('ms', '')))}</td><td>{e(r.get('guard') or r.get('error') or '')}</td>"
        f"<td class='t'>{e(r.get('text', ''))}</td></tr>"
        for r in rows)
    live_block = (f"<h2>Your real dictations (latency log)</h2><p class='note'>Release-to-text time "
                  f"measured on your PC, grouped by engine. For reference, under 1 second keeps your "
                  f"flow of thought and 0.1 second feels instant (Nielsen Norman Group).</p><table><tr><th>Mode</th><th>Speech</th><th>Cleanup</th><th>n</th>"
                  f"<th>Total p50</th><th>Total p90</th><th>Speech p50</th><th>Cleanup p50</th>"
                  f"<th>Paste p50</th><th>Cost / 1,000</th></tr>{lrows}</table>") if live else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Scribe Plus benchmark</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--dim:#78716c;--line:#e7e5e4;--a:#2563eb;--b:#93c5fd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#1c1917;--fg:#f5f5f4;--dim:#a8a29e;--line:#44403c;--a:#60a5fa;--b:#1e3a8a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px 16px;}}
main{{max-width:1040px;margin:auto}} h1{{font-size:22px;margin:0 0 4px}} h2{{font-size:17px;margin:32px 0 6px}}
.note{{color:var(--dim);margin:0 0 12px}} table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{color:var(--dim);font-weight:600}} td.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.m{{color:var(--dim);font-size:12px}} td.t{{max-width:460px}}
.bar{{position:relative;height:10px;min-width:120px;background:transparent}}
.bar span{{position:absolute;left:0;top:0;height:10px;border-radius:5px}}
.bar .p90{{background:var(--b)}} .bar .p50{{background:var(--a)}}
details{{margin-top:24px}} .wrap{{overflow-x:auto}}
</style></head><body><main>
<h1>Scribe Plus benchmark</h1>
<p class="note">{n_clips} of your saved clips, replayed on {datetime.now():%Y-%m-%d %H:%M}. Latency is per request
on a warm connection (dark bar median, light bar 90th percentile). Speech "disagreement" is word error rate
against Whisper Large v3, a proxy since there is no human transcript. Cleanup "guard" counts outputs the
safety check rejected as answers or rewrites.</p>
<div class="wrap"><table><tr><th>Stage</th><th>Engine</th><th>p50 ms</th><th>p90 ms</th><th></th>
<th>Cost / 1,000</th><th>Disagree / guard</th><th>Runs</th></tr>{srows}</table></div>
{live_block}
<details><summary>Every result</summary><div class="wrap"><table><tr><th>Clip</th><th>Stage</th><th>Engine</th>
<th>ms</th><th>Guard / error</th><th>Text</th></tr>{detail}</table></div></details>
</main></body></html>"""


if __name__ == "__main__":
    sys.exit(main())
