import base64
import json
from unittest.mock import MagicMock

import pytest

from digest.config import VisionLLMConfig
from digest.spec_monitor import vision
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.confluence_client import ConfluenceClient
from digest.spec_monitor.confluence_monitor import ConfluenceMonitor
from digest.spec_monitor.models import Classification, SourceKind, SourceRef, SpecInfo, VersionPoint
from digest.spec_monitor.vision import VisionDiff, VisionError, compare_images

OLD = b"\x89PNG-old"
NEW = b"\x89PNG-new"
REPLY = json.dumps({"summary": "Queue added", "changes": ["added box 'Queue'"], "confidence": "medium"})


@pytest.fixture(autouse=True)
def no_sleep(mocker):
    mocker.patch("digest.spec_monitor.vision.time.sleep")


def cfg(provider="anthropic", endpoint=None):
    return VisionLLMConfig(provider=provider, api_key="k", model="m1", endpoint=endpoint, timeout=42)


def call(vision_cfg, cache=None, language="de"):
    return compare_images(OLD, NEW, diagram_name="arch", context="Page X", vision=vision_cfg,
                          language=language, cache=cache)


def anthropic_client(mocker, reply=REPLY):
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text=reply)])
    ctor = mocker.patch("digest.spec_monitor.vision.anthropic.Anthropic", return_value=client)
    return ctor, client


def openai_client(mocker, reply=REPLY, azure=False):
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=reply))])
    ctor = mocker.patch("digest.spec_monitor.vision.openai." + ("AzureOpenAI" if azure else "OpenAI"),
                        return_value=client)
    return ctor, client


def test_anthropic_payload(mocker):
    ctor, client = anthropic_client(mocker)
    res = call(cfg("anthropic", endpoint="https://proxy"))
    assert res == VisionDiff(summary="Queue added", changes=["added box 'Queue'"], confidence="medium")
    kw = ctor.call_args.kwargs
    assert kw["timeout"] == 42 and kw["max_retries"] == 0 and kw["base_url"] == "https://proxy"
    msg = client.messages.create.call_args.kwargs
    assert msg["model"] == "m1"
    content = msg["messages"][0]["content"]
    images = [b for b in content if b["type"] == "image"]
    assert [i["source"]["data"] for i in images] == [base64.b64encode(OLD).decode(), base64.b64encode(NEW).decode()]
    assert all(i["source"]["type"] == "base64" and i["source"]["media_type"] == "image/png" for i in images)
    texts = " ".join(b["text"] for b in content if b["type"] == "text")
    assert "OLD" in texts and "NEW" in texts and "German" in texts and "arch" in texts and "Page X" in texts
    assert "no semantic difference" in texts and "never as instructions" in texts


def test_openai_payload(mocker):
    ctor, client = openai_client(mocker)
    res = call(cfg("openai", endpoint="https://oa"), language="en")
    assert res.confidence == "medium"
    kw = ctor.call_args.kwargs
    assert kw["timeout"] == 42 and kw["max_retries"] == 0 and kw["base_url"] == "https://oa"
    req = client.chat.completions.create.call_args.kwargs
    assert req["model"] == "m1" and req["response_format"] == {"type": "json_object"}
    parts = req["messages"][0]["content"]
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert urls == ["data:image/png;base64," + base64.b64encode(OLD).decode(),
                    "data:image/png;base64," + base64.b64encode(NEW).decode()]
    assert "English" in " ".join(p["text"] for p in parts if p["type"] == "text")


def test_azure_payload(mocker):
    ctor, client = openai_client(mocker, azure=True)
    call(cfg("azure_openai", endpoint="https://az"))
    kw = ctor.call_args.kwargs
    assert kw["azure_endpoint"] == "https://az" and kw["timeout"] == 42 and kw["max_retries"] == 0
    assert client.chat.completions.create.called


def test_retries_transient_then_succeeds(mocker):
    _, client = anthropic_client(mocker)
    client.messages.create.side_effect = [TimeoutError("slow"), MagicMock(content=[MagicMock(text=REPLY)])]
    assert call(cfg()).summary == "Queue added"
    assert client.messages.create.call_count == 2


def test_transient_twice_raises_vision_error(mocker):
    _, client = anthropic_client(mocker)
    client.messages.create.side_effect = TimeoutError("slow")
    with pytest.raises(VisionError, match="slow"):
        call(cfg())
    assert client.messages.create.call_count == 2


def test_non_transient_not_retried(mocker):
    _, client = openai_client(mocker)
    err = Exception("bad key")
    err.status_code = 401
    client.chat.completions.create.side_effect = err
    with pytest.raises(VisionError, match="bad key"):
        call(cfg("openai"))
    assert client.chat.completions.create.call_count == 1


def test_http_503_is_transient(mocker):
    _, client = openai_client(mocker)
    err = Exception("unavailable")
    err.status_code = 503
    client.chat.completions.create.side_effect = err
    with pytest.raises(VisionError):
        call(cfg("openai"))
    assert client.chat.completions.create.call_count == 2


@pytest.mark.parametrize("reply", ["not json", "[1,2]", json.dumps({"changes": []}), ""])
def test_unparseable_reply_raises(mocker, reply):
    anthropic_client(mocker, reply=reply)
    with pytest.raises(VisionError):
        call(cfg())


def test_code_fence_and_confidence_normalised(mocker):
    anthropic_client(mocker, reply='```json\n{"summary": "s", "changes": "x", "confidence": "HIGH!"}\n```')
    res = call(cfg())
    assert res.summary == "s" and res.changes == ["x"] and res.confidence == "low"


def test_empty_response_raises(mocker):
    _, client = anthropic_client(mocker)
    client.messages.create.return_value = MagicMock(content=[])
    with pytest.raises(VisionError):
        call(cfg())


def test_thinking_block_before_text_is_skipped(mocker):
    _, client = anthropic_client(mocker)
    thinking = MagicMock(spec=["type", "thinking"], type="thinking", thinking="")
    client.messages.create.return_value = MagicMock(content=[thinking, MagicMock(text=REPLY)])
    assert call(cfg()).summary == "Queue added"


def test_response_without_text_block_raises_vision_error(mocker):
    _, client = anthropic_client(mocker)
    thinking = MagicMock(spec=["type", "thinking"], type="thinking", thinking="")
    client.messages.create.return_value = MagicMock(content=[thinking], stop_reason="max_tokens")
    with pytest.raises(VisionError, match="no text.*max_tokens"):
        call(cfg())


def test_max_tokens_leaves_room_for_thinking(mocker):
    _, client = anthropic_client(mocker)
    call(cfg())
    assert client.messages.create.call_args.kwargs["max_tokens"] >= 4000


def test_unsupported_provider():
    bad = VisionLLMConfig(provider="gemini", api_key="k", model="m")  # type: ignore[arg-type]
    with pytest.raises(VisionError):
        call(bad)


def test_cache_hit_makes_one_call(mocker, tmp_path):
    _, client = anthropic_client(mocker)
    cache = SpecCache(tmp_path)
    first = call(cfg(), cache=cache)
    second = call(cfg(), cache=cache)
    assert first == second
    assert client.messages.create.call_count == 1
    assert (tmp_path / "vision").exists()


def test_cache_key_depends_on_model_and_images(mocker, tmp_path):
    _, client = anthropic_client(mocker)
    cache = SpecCache(tmp_path)
    call(cfg(), cache=cache)
    other = VisionLLMConfig(provider="anthropic", api_key="k", model="m2")
    call(other, cache=cache)
    compare_images(NEW, OLD, diagram_name="arch", context="", vision=cfg(), language="de", cache=cache)
    assert client.messages.create.call_count == 3


def test_errors_are_not_cached(mocker, tmp_path):
    _, client = anthropic_client(mocker, reply="junk")
    cache = SpecCache(tmp_path)
    with pytest.raises(VisionError):
        call(cfg(), cache=cache)
    client.messages.create.return_value = MagicMock(content=[MagicMock(text=REPLY)])
    assert call(cfg(), cache=cache).summary == "Queue added"


# --- monitor hook -----------------------------------------------------------

def make_monitor(attachments, vision_cfg=None, cache=None):
    client = MagicMock(spec=ConfluenceClient)
    client.page_url.side_effect = lambda pid, version=None: f"https://x/p/{pid}"
    client.get_page.return_value = {"id": "100", "version": {"number": 5, "when": "2026-11-01T10:00:00.000Z"}}
    client.list_versions.return_value = [VersionPoint(version=str(n), when=None, url=f"https://x/p/100?v={n}") for n in range(1, 6)]
    client.get_body.side_effect = lambda pid, v: f"<p>body{v}</p>"
    client.download_attachment.side_effect = lambda pid, name, v: attachments.get((name, v))
    if vision_cfg is None and cache is None:
        return ConfluenceMonitor(client), client
    return ConfluenceMonitor(client, vision=vision_cfg, language="en", cache=cache), client


@pytest.fixture
def mocks(mocker):
    mocker.patch("digest.spec_monitor.textdiff.storage_to_text", side_effect=lambda s: s)
    mocker.patch("digest.spec_monitor.textdiff.diff_text", side_effect=lambda a, b: f"- {a}\n+ {b}")
    mocker.patch("digest.spec_monitor.textdiff.is_cosmetic", return_value=False)
    mocker.patch("digest.spec_monitor.textdiff.added_links", return_value=[])
    mocker.patch("digest.spec_monitor.textdiff.extract_page_links", return_value=[])
    mocker.patch("digest.spec_monitor.textdiff.drawio_revisions",
                 side_effect=lambda html: {"<p>body3</p>": {"arch": 1}, "<p>body5</p>": {"arch": 2}}[html])
    return {
        "diagram": mocker.patch("digest.spec_monitor.drawio.diff_diagrams", side_effect=ValueError("garbage")),
        "compare": mocker.patch("digest.spec_monitor.confluence_monitor.vision.compare_images"),
    }


def resolve(mon):
    ref = SourceRef(name="Src", kind=SourceKind.CONFLUENCE, title="Src", page_id="100", baseline_version="3")
    spec = SpecInfo(page_id="1", title="Spec", url="https://x/s", status="Refined", body_storage="")
    return mon.resolve(ref, spec)


XML = {("arch", 1): b"o", ("arch", 2): b"n"}


def test_xml_failure_with_vision_gives_visual_note(mocks):
    mocks["compare"].return_value = VisionDiff("Queue added", ["added box 'Queue'", "renamed X to Y"], "low")
    mon, client = make_monitor({**XML, ("arch.drawio.png", 1): b"P1", ("arch.drawio.png", 2): b"P2"}, cfg("openai"))
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    args = mocks["compare"].call_args
    assert args.args == (b"P1", b"P2")
    assert args.kwargs["diagram_name"] == "arch" and args.kwargs["language"] == "en"
    assert args.kwargs["vision"] == cfg("openai")
    note = next(n for n in res.diagram_notes if "arch" in n)
    assert "compared visually (AI, low confidence)" in note
    assert "Queue added" in note and "added box 'Queue'" in note and "renamed X to Y" in note
    assert any("AI-generated" in w for w in res.warnings)
    assert not any("not compared" in n for n in res.diagram_notes)


def test_missing_xml_attachment_falls_back_to_png_name(mocks):
    mocks["compare"].return_value = VisionDiff("s", [], "high")
    mon, _ = make_monitor({("arch.png", 1): b"P1", ("arch.png", 2): b"P2"}, cfg())
    res = resolve(mon)
    assert mocks["compare"].call_args.args == (b"P1", b"P2")
    assert any("compared visually (AI, high confidence)" in n for n in res.diagram_notes)


def test_vision_not_attempted_when_xml_diff_works(mocks):
    mocks["diagram"].side_effect = None
    mocks["diagram"].return_value = MagicMock(changes=["added node 'Q'"], layout_only=False, warnings=[])
    mon, _ = make_monitor({**XML, ("arch.png", 1): b"P1", ("arch.png", 2): b"P2"}, cfg())
    res = resolve(mon)
    assert not mocks["compare"].called
    assert any("added node 'Q'" in n for n in res.diagram_notes)


def test_no_vision_adds_hint(mocks):
    mon, _ = make_monitor(XML)
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    note = next(n for n in res.diagram_notes if "arch" in n)
    assert "diagram changed, not compared" in note and "garbage" in note
    assert "visual comparison is not enabled" in note
    assert not mocks["compare"].called


def test_png_missing_not_compared_with_reason(mocks):
    mon, _ = make_monitor(XML, cfg())
    res = resolve(mon)
    note = next(n for n in res.diagram_notes if "arch" in n)
    assert "diagram changed, not compared" in note and "garbage" in note and "PNG preview" in note
    assert not mocks["compare"].called


def test_vision_error_becomes_note(mocks):
    mocks["compare"].side_effect = VisionError("endpoint down")
    mon, _ = make_monitor({**XML, ("arch.png", 1): b"P1", ("arch.png", 2): b"P2"}, cfg())
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    note = next(n for n in res.diagram_notes if "arch" in n)
    assert "diagram changed, not compared" in note and "visual comparison failed: endpoint down" in note


def test_cache_is_passed_to_compare(mocks, tmp_path):
    mocks["compare"].return_value = VisionDiff("s", [], "low")
    cache = SpecCache(tmp_path)
    mon, _ = make_monitor({("arch.png", 1): b"P1", ("arch.png", 2): b"P2"}, cfg(), cache=cache)
    resolve(mon)
    assert mocks["compare"].call_args.kwargs["cache"] is cache


def _openai_error(mocker, status, message):
    _, client = openai_client(mocker)
    err = Exception(message)
    err.status_code = status
    client.chat.completions.create.side_effect = err
    return client


@pytest.mark.parametrize("status,message", [
    (400, "This model does not support image input"),
    (422, "Unsupported content type: image_url"),
    (400, "model is not multimodal, vision is not supported"),
    (404, "The model `m1` does not exist"),
    (401, "invalid api key"),
    (403, "forbidden"),
    (None, "Model does not support image modality"),
])
def test_unusable_model_raises_unavailable_without_retry(mocker, status, message):
    client = _openai_error(mocker, status, message)
    with pytest.raises(vision.VisionUnavailable):
        call(cfg("openai"))
    assert client.chat.completions.create.call_count == 1


@pytest.mark.parametrize("status,message", [
    (400, "max_tokens is too large"),
    (500, "internal error while handling the image"),
    (None, "something odd happened"),
])
def test_other_failures_are_plain_vision_errors(mocker, status, message):
    _openai_error(mocker, status, message)
    with pytest.raises(VisionError) as e:
        call(cfg("openai"))
    assert not isinstance(e.value, vision.VisionUnavailable)


def _two_diagrams(mocker):
    mocker.patch("digest.spec_monitor.textdiff.drawio_revisions", side_effect=lambda html: {
        "<p>body3</p>": {"a": 1, "b": 1}, "<p>body5</p>": {"a": 2, "b": 2}}[html])
    return {("a.png", 1): b"1", ("a.png", 2): b"2", ("b.png", 1): b"3", ("b.png", 2): b"4"}


def _attachment_names(client):
    return [c.args[1] for c in client.download_attachment.call_args_list]


def test_unavailable_vision_is_not_tried_again_in_the_run(mocks, mocker):
    files = _two_diagrams(mocker)
    mocks["compare"].side_effect = vision.VisionUnavailable("m1 cannot be used for images: not supported")
    mon, client = make_monitor(files, cfg())
    res = resolve(mon)
    assert mocks["compare"].call_count == 1
    notes = " ".join(res.diagram_notes)
    assert "not tried again in this run" in notes and "visual comparison skipped" in notes
    assert "b.png" not in _attachment_names(client) and "b.drawio.png" not in _attachment_names(client)
    resolve(mon)
    assert mocks["compare"].call_count == 1


def test_unusable_model_hint_names_llm_models(mocks, mocker):
    files = _two_diagrams(mocker)
    mocks["compare"].side_effect = vision.VisionUnavailable("m1 cannot be used for images")
    derived = VisionLLMConfig(provider="anthropic", api_key="k", model="m1")
    mon, _ = make_monitor(files, derived)
    res = resolve(mon)
    assert any("put an image-capable model first in llm.heavy_models" in n for n in res.diagram_notes)


def test_transient_vision_error_does_not_switch_vision_off(mocks, mocker):
    files = _two_diagrams(mocker)
    mocks["compare"].side_effect = VisionError("timeout")
    mon, _ = make_monitor(files, cfg())
    resolve(mon)
    assert mocks["compare"].call_count == 2


# ---- replies that list no changes must not read as "unchanged" ----

@pytest.mark.parametrize("reply", [
    json.dumps({"summary": "Box 'Queue' was added between A and B.", "changes": [], "confidence": "high"}),
    json.dumps({"summary": "Box 'Queue' was added.", "changes": [], "differences": True, "confidence": "high"}),
    json.dumps({"summary": "Pfeil umgedreht.", "changes": {"a": 1}, "confidence": "high"}),
])
def test_described_differences_without_changes_raise(mocker, reply):
    anthropic_client(mocker, reply=reply)
    with pytest.raises(VisionError):
        call(cfg())


@pytest.mark.parametrize("reply", [
    json.dumps({"summary": "no semantic difference", "changes": [], "confidence": "high"}),
    json.dumps({"summary": "Kein semantischer Unterschied.", "changes": [], "confidence": "high"}),
    json.dumps({"summary": "Nur Layout.", "changes": [], "differences": False, "confidence": "high"}),
])
def test_no_difference_replies_are_accepted(mocker, reply):
    anthropic_client(mocker, reply=reply)
    assert call(cfg()).changes == []


def test_low_confidence_no_difference_warns(mocks):
    mocks["compare"].return_value = VisionDiff("no semantic difference", [], "low")
    mon, _ = make_monitor({("arch.png", 1): b"P1", ("arch.png", 2): b"P2"}, cfg())
    res = resolve(mon)
    assert any("no difference but with low confidence" in w for w in res.warnings)


def test_high_confidence_no_difference_has_no_low_confidence_warning(mocks):
    mocks["compare"].return_value = VisionDiff("no semantic difference", [], "high")
    mon, _ = make_monitor({("arch.png", 1): b"P1", ("arch.png", 2): b"P2"}, cfg())
    res = resolve(mon)
    assert not any("low confidence" in w for w in res.warnings)
