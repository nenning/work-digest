"""Per-run usage statistics: LLM calls (by purpose and model, with tokens), HTTP requests (by host)
and cache hits. Collection is off until start() is called (the spec monitor does so), so every
recording function is a cheap no-op in the personal digest and the management summary."""
import html
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional, Tuple
from urllib.parse import urlsplit


@dataclass
class LlmRow:
    calls: int = 0
    failed: int = 0
    unusable: int = 0            # reply arrived but was not valid for the requested shape (re-asked / next model)
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    with_usage: int = 0          # calls whose response reported token usage


@dataclass
class HttpRow:
    requests: int = 0
    attempts: int = 0
    retried: int = 0             # requests that needed more than one attempt
    failed: int = 0              # still failing after every attempt
    http_errors: int = 0         # final status >= 400 (404 etc.)
    seconds: float = 0.0


@dataclass
class RunStats:
    started: float = field(default_factory=time.monotonic)
    llm: Dict[Tuple[str, str], LlmRow] = field(default_factory=lambda: defaultdict(LlmRow))
    http: Dict[str, HttpRow] = field(default_factory=lambda: defaultdict(HttpRow))
    cache: Dict[str, list] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))   # namespace -> [hits, misses]
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


_stats: Optional[RunStats] = None
_tls = threading.local()


def start() -> RunStats:
    global _stats
    _stats = RunStats()
    return _stats


def current() -> Optional[RunStats]:
    return _stats


def stop() -> Optional[RunStats]:
    global _stats
    stats, _stats = _stats, None
    return stats


def note_tokens(tokens_in, tokens_out) -> None:
    """Called by the low-level LLM clients with the response's usage; attributed to the active llm_call()."""
    call = getattr(_tls, "call", None)
    if call is None:
        return
    if isinstance(tokens_in, int) and isinstance(tokens_out, int):
        call["in"], call["out"] = tokens_in, tokens_out


def mark_unusable(purpose: str, model: str) -> None:
    stats = _stats
    if stats is not None:
        with stats.lock:
            stats.llm[(purpose, model)].unusable += 1


@contextmanager
def llm_call(purpose: str, model: str) -> Iterator[None]:
    stats = _stats
    if stats is None:
        yield
        return
    call = {"in": None, "out": None}
    _tls.call = call
    t0 = time.monotonic()
    ok = False
    try:
        yield
        ok = True
    finally:
        _tls.call = None
        with stats.lock:
            row = stats.llm[(purpose, model)]
            row.calls += 1
            row.seconds += time.monotonic() - t0
            if not ok:
                row.failed += 1
            if call["in"] is not None:
                row.with_usage += 1
                row.tokens_in += call["in"]
                row.tokens_out += call["out"]


def record_http(url: str, *, attempts: int, status: Optional[int], failed: bool, seconds: float) -> None:
    stats = _stats
    if stats is None:
        return
    host = urlsplit(url).netloc or url
    with stats.lock:
        row = stats.http[host]
        row.requests += 1
        row.attempts += attempts
        row.seconds += seconds
        if attempts > 1:
            row.retried += 1
        if failed:
            row.failed += 1
        elif status is not None and status >= 400:
            row.http_errors += 1


def record_cache(namespace: str, hit: bool) -> None:
    stats = _stats
    if stats is None:
        return
    with stats.lock:
        stats.cache[namespace][0 if hit else 1] += 1


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _table(header: list, rows: list, left: int) -> list:
    """Aligned text table; the first `left` columns are left-aligned (labels), the rest right-aligned."""
    widths = [max(len(str(r[i])) for r in [header] + rows) for i in range(len(header))]

    def fmt(r):
        cells = [str(c).ljust(widths[i]) if i < left else str(c).rjust(widths[i]) for i, c in enumerate(r)]
        return "  " + "  ".join(cells).rstrip()

    return [fmt(header)] + [fmt(r) for r in rows]


def _llm_total(llm: Dict[Tuple[str, str], LlmRow]) -> LlmRow:
    total = LlmRow()
    for r in llm.values():
        total.calls += r.calls
        total.failed += r.failed
        total.unusable += r.unusable
        total.seconds += r.seconds
        total.tokens_in += r.tokens_in
        total.tokens_out += r.tokens_out
        total.with_usage += r.with_usage
    return total


def _llm_by_model(llm: Dict[Tuple[str, str], LlmRow]) -> Dict[str, LlmRow]:
    by_model: Dict[str, LlmRow] = defaultdict(LlmRow)
    for (_, model), r in llm.items():
        m = by_model[model]
        m.calls += r.calls
        m.tokens_in += r.tokens_in
        m.tokens_out += r.tokens_out
        m.with_usage += r.with_usage
    return by_model


def format_report(stats: RunStats) -> str:
    lines = [f"\nRun statistics (duration {_duration(stats.elapsed)})"]

    llm = dict(stats.llm)
    if llm:
        total = _llm_total(llm)
        tokens = (f"tokens in {total.tokens_in:,} / out {total.tokens_out:,}" if total.with_usage
                  else "tokens not reported")
        extra = f", {total.failed} failed" if total.failed else ""
        extra += f", {total.unusable} unusable reply(ies)" if total.unusable else ""
        lines.append(f"LLM calls: {total.calls}{extra}; {tokens}")
        rows = []
        for (purpose, model), r in sorted(llm.items()):
            rows.append([purpose, model, r.calls, r.failed, r.unusable,
                         f"{r.tokens_in:,}" if r.with_usage else "-", f"{r.tokens_out:,}" if r.with_usage else "-",
                         f"{r.seconds / r.calls:.1f}" if r.calls else "-"])
        lines += _table(["purpose", "model", "calls", "failed", "unusable", "tok in", "tok out", "avg s"], rows, left=2)
        by_model = _llm_by_model(llm)
        if len(by_model) > 1:
            lines.append("  per model: " + "; ".join(
                f"{model} {r.calls} call(s)" + (f", {r.tokens_in:,} in / {r.tokens_out:,} out" if r.with_usage else "")
                for model, r in sorted(by_model.items())))
    else:
        lines.append("LLM calls: 0")

    http = dict(stats.http)
    if http:
        req = sum(r.requests for r in http.values())
        retried = sum(r.retried for r in http.values())
        failed = sum(r.failed for r in http.values())
        errors = sum(r.http_errors for r in http.values())
        parts = [f"{retried} retried"] if retried else []
        parts += [f"{failed} failed"] if failed else []
        parts += [f"{errors} HTTP error status"] if errors else []
        lines.append(f"HTTP requests: {req}" + (f" ({', '.join(parts)})" if parts else ""))
        lines += _table(["host", "requests", "attempts", "retried", "failed", "errors", "avg s"], [
            [host, r.requests, r.attempts, r.retried, r.failed, r.http_errors,
             f"{r.seconds / r.requests:.2f}"] for host, r in sorted(http.items())], left=1)
    else:
        lines.append("HTTP requests: 0")

    cache = {ns: tuple(v) for ns, v in stats.cache.items()}
    if cache:
        hits = sum(v[0] for v in cache.values())
        misses = sum(v[1] for v in cache.values())
        lines.append(f"Cache: {hits} hit(s), {misses} miss(es)")
        lines.append("  " + "; ".join(f"{ns} {h}/{h + m}" for ns, (h, m) in sorted(cache.items())) + "  (hits/lookups)")
    return "\n".join(lines)


def format_html(stats: RunStats) -> str:
    """The same statistics as format_report(), as a `.block` for the spec-monitor preview page (its stylesheet
    provides `.block`, `.meta` and `table.t`)."""
    esc = html.escape

    right = ' style="text-align:right"'

    def table(header: list, rows: list, left: int) -> str:
        head = "".join(f"<th{'' if i < left else right}>{esc(h)}</th>" for i, h in enumerate(header))
        body = "".join(
            "<tr>" + "".join(
                f"<td{'' if i < left else right}>{esc(str(c))}</td>" for i, c in enumerate(r)
            ) + "</tr>" for r in rows)
        return f'<table class="t" width="100%" cellpadding="0" cellspacing="0"><tr>{head}</tr>{body}</table>'

    parts = ['<div class="block" id="run-statistics">', "<h2>Run statistics</h2>",
             f'<p class="meta">Duration {esc(_duration(stats.elapsed))} (up to this preview; dry run only, not part of the mail)</p>']

    llm = dict(stats.llm)
    parts.append("<h3>LLM calls</h3>")
    if llm:
        total = _llm_total(llm)
        tokens = (f"tokens in {total.tokens_in:,} / out {total.tokens_out:,}" if total.with_usage
                  else "tokens not reported")
        extra = f", {total.failed} failed" if total.failed else ""
        extra += f", {total.unusable} unusable reply(ies)" if total.unusable else ""
        parts.append(f'<p class="meta">{total.calls} call(s){esc(extra)}; {esc(tokens)}; '
                     f'{esc(_duration(total.seconds))} spent waiting</p>')
        parts.append(table(["purpose", "model", "calls", "failed", "unusable", "tok in", "tok out", "avg s"], [
            [purpose, model, r.calls, r.failed, r.unusable,
             f"{r.tokens_in:,}" if r.with_usage else "-", f"{r.tokens_out:,}" if r.with_usage else "-",
             f"{r.seconds / r.calls:.1f}" if r.calls else "-"]
            for (purpose, model), r in sorted(llm.items())], left=2))
        by_model = _llm_by_model(llm)
        if len(by_model) > 1:
            parts.append("<h3>Per model</h3>")
            parts.append(table(["model", "calls", "tok in", "tok out"], [
                [model, r.calls, f"{r.tokens_in:,}" if r.with_usage else "-", f"{r.tokens_out:,}" if r.with_usage else "-"]
                for model, r in sorted(by_model.items())], left=1))
    else:
        parts.append('<p class="meta">No LLM calls.</p>')

    http = dict(stats.http)
    parts.append("<h3>HTTP requests</h3>")
    if http:
        req = sum(r.requests for r in http.values())
        retried = sum(r.retried for r in http.values())
        failed = sum(r.failed for r in http.values())
        errors = sum(r.http_errors for r in http.values())
        extra = [f"{retried} retried"] if retried else []
        extra += [f"{failed} failed"] if failed else []
        extra += [f"{errors} HTTP error status"] if errors else []
        parts.append(f'<p class="meta">{req} request(s){esc(" (" + ", ".join(extra) + ")") if extra else ""}</p>')
        parts.append(table(["host", "requests", "attempts", "retried", "failed", "errors", "avg s"], [
            [host, r.requests, r.attempts, r.retried, r.failed, r.http_errors, f"{r.seconds / r.requests:.2f}"]
            for host, r in sorted(http.items())], left=1))
    else:
        parts.append('<p class="meta">No HTTP requests.</p>')

    cache = {ns: tuple(v) for ns, v in stats.cache.items()}
    if cache:
        hits = sum(v[0] for v in cache.values())
        misses = sum(v[1] for v in cache.values())
        parts.append("<h3>Cache</h3>")
        parts.append(f'<p class="meta">{hits} hit(s), {misses} miss(es)</p>')
        parts.append(table(["namespace", "hits", "misses", "lookups"], [
            [ns, h, m, h + m] for ns, (h, m) in sorted(cache.items())], left=1))

    parts.append("</div>")
    return "\n".join(parts)
