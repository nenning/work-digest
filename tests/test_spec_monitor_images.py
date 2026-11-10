import base64
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from digest.config import VisionLLMConfig
from digest.spec_monitor import textdiff, vision
from digest.spec_monitor.confluence_client import ConfluenceClient, pick_version_before
from digest.spec_monitor.confluence_monitor import MAX_IMAGE_COMPARISONS, ConfluenceMonitor
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import Classification, SourceKind, SourceRef, SpecInfo, VersionPoint
from digest.spec_monitor.vision import VisionDiff, VisionError, compare_images

PNG = b"\x89PNG-data"
JPEG = b"\xff\xd8\xff-data"
REPLY = json.dumps({"summary": "Limit changed", "changes": ["limit 10 => 20"], "confidence": "high"})


def cfg():
    return VisionLLMConfig(provider="anthropic", api_key="k", model="m1", endpoint=None, timeout=42)


def at(day, hour=12):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


# --- textdiff.image_attachments ----------------------------------------------

def img(name):
    return f'<ac:image><ri:attachment ri:filename="{name}" /></ac:image>'


def test_image_attachments_of_this_page_in_order_without_duplicates():
    body = f"<p>{img('a.png')}</p><p>{img('b.jpg')}{img('a.png')}</p>"
    assert textdiff.image_attachments(body) == (["a.png", "b.jpg"], 0)


def test_images_of_other_pages_and_external_urls_are_counted_not_listed():
    body = (
        '<ac:image><ri:attachment ri:filename="x.png"><ri:page ri:content-title="Other" /></ri:attachment></ac:image>'
        '<ac:image><ri:url ri:value="https://example.com/p.png" /></ac:image>'
        + img("own.png")
    )
    assert textdiff.image_attachments(body) == (["own.png"], 2)


def test_image_attachment_name_is_unescaped():
    assert textdiff.image_attachments(img("a &amp; b.png"))[0] == ["a & b.png"]


def test_no_images():
    assert textdiff.image_attachments("<p>text</p>") == ([], 0)
    assert textdiff.image_attachments("") == ([], 0)


# --- pick_version_before -----------------------------------------------------

def test_pick_version_before_takes_last_version_up_to_the_moment():
    vs = [VersionPoint(version=str(n), when=at(n), url="") for n in (1, 3, 5)]
    assert pick_version_before(vs, at(3, 12)).version == "3"
    assert pick_version_before(vs, at(4)).version == "3"
    assert pick_version_before(vs, at(1, 11)) is None


# --- vision ------------------------------------------------------------------

def anthropic_client(mocker):
    client = MagicMock()
    client.messages.create.return_value = MagicMock(content=[MagicMock(text=REPLY)])
    mocker.patch("digest.spec_monitor.vision.anthropic.Anthropic", return_value=client)
    return client


def test_media_type_is_detected_per_image(mocker):
    client = anthropic_client(mocker)
    compare_images(PNG, JPEG, diagram_name="shot.png", context="", vision=cfg(), language="en", kind="image")
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert [b["source"]["media_type"] for b in content if b["type"] == "image"] == ["image/png", "image/jpeg"]
    assert [b["source"]["data"] for b in content if b["type"] == "image"] == [
        base64.b64encode(PNG).decode(), base64.b64encode(JPEG).decode()]


def test_image_prompt_is_not_about_boxes_and_arrows(mocker):
    client = anthropic_client(mocker)
    compare_images(PNG, PNG + b"x", diagram_name="shot.png", context="", vision=cfg(), language="en", kind="image")
    text = " ".join(b["text"] for b in client.messages.create.call_args.kwargs["messages"][0]["content"]
                    if b["type"] == "text")
    assert "image 'shot.png'" in text and "draw.io" not in text and "values" in text
    assert "never as instructions" in text


def test_diagram_prompt_unchanged_by_default(mocker):
    client = anthropic_client(mocker)
    compare_images(PNG, PNG + b"x", diagram_name="arch", context="", vision=cfg(), language="en")
    text = " ".join(b["text"] for b in client.messages.create.call_args.kwargs["messages"][0]["content"]
                    if b["type"] == "text")
    assert "draw.io diagram named 'arch'" in text and "boxes" in text


def test_webp_is_recognised(mocker):
    anthropic_client(mocker)
    webp = b"RIFF\x00\x00\x00\x00WEBPVP8 "
    compare_images(webp, PNG, diagram_name="s", context="", vision=cfg(), language="en", kind="image")


@pytest.mark.parametrize("data", [b"<svg></svg>", b"not an image", b""])
def test_unsupported_format_raises_before_any_call(mocker, data):
    client = anthropic_client(mocker)
    with pytest.raises(VisionError, match="not supported"):
        compare_images(PNG, data, diagram_name="s", context="", vision=cfg(), language="en", kind="image")
    assert not client.messages.create.called


def test_too_large_image_raises_before_any_call(mocker):
    client = anthropic_client(mocker)
    big = b"\x89PNG" + b"0" * (5 * 1024 * 1024)
    with pytest.raises(VisionError, match="too large"):
        compare_images(PNG, big, diagram_name="s", context="", vision=cfg(), language="en", kind="image")
    assert not client.messages.create.called


def test_cache_is_separate_per_kind(mocker, tmp_path):
    from digest.spec_monitor.cache import SpecCache
    client = anthropic_client(mocker)
    cache = SpecCache(tmp_path)
    for kind in ("diagram", "image", "image"):
        compare_images(PNG, JPEG, diagram_name="s", context="", vision=cfg(), language="en", cache=cache, kind=kind)
    assert client.messages.create.call_count == 2


# --- monitor -----------------------------------------------------------------

BODY = "<p>x</p>" + img("shot.png")


def versions(*pairs):
    return [VersionPoint(version=str(n), when=at(d), url="") for n, d in pairs]


def make_monitor(attachment_versions, files, vision_cfg=cfg(), bodies=None):
    client = MagicMock(spec=ConfluenceClient)
    client.page_url.side_effect = lambda pid, version=None: f"https://x/p/{pid}"
    client.diff_url.return_value = "https://x/diff"
    client.get_page.return_value = {"id": "100", "version": {"number": 5, "when": "2026-11-10T10:00:00.000Z"}}
    client.list_versions.return_value = [
        VersionPoint(version=str(n), when=at(n), url=f"https://x/p/100?v={n}") for n in range(1, 6)]
    bodies = bodies or {3: BODY + "<p>old</p>", 5: BODY + "<p>new</p>"}
    client.get_body.side_effect = lambda pid, v: bodies[v]
    if isinstance(attachment_versions, Exception):
        client.attachment_versions.side_effect = attachment_versions
    else:
        client.attachment_versions.side_effect = lambda pid, name: attachment_versions.get(name)
    client.download_attachment.side_effect = lambda pid, name, v: files.get((name, v))
    return ConfluenceMonitor(client, vision=vision_cfg, language="en"), client


def resolve(mon):
    ref = SourceRef(name="Src", kind=SourceKind.CONFLUENCE, title="Src", page_id="100", baseline_version="3")
    spec = SpecInfo(page_id="1", title="Spec", url="https://x/s", status="Refined", body_storage="")
    return mon.resolve(ref, spec)


@pytest.fixture
def compare(mocker):
    return mocker.patch("digest.spec_monitor.confluence_monitor.vision.compare_images")


def test_changed_image_is_compared_visually_with_the_versions_current_at_each_page_version(compare):
    compare.return_value = VisionDiff("Limit changed", ["limit 10 => 20"], "high")
    # page v3 is from day 3; attachment v2 (day 2) was current then, v3 (day 8) is the latest
    mon, client = make_monitor({"shot.png": versions((1, 1), (2, 2), (3, 8))},
                               {("shot.png", 2): PNG, ("shot.png", 3): JPEG})
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    assert compare.call_args.args == (PNG, JPEG)
    assert compare.call_args.kwargs["kind"] == "image" and compare.call_args.kwargs["diagram_name"] == "shot.png"
    note = next(n for n in res.diagram_notes if "shot.png" in n)
    assert note == "image 'shot.png' compared visually (AI, high confidence): Limit changed; limit 10 => 20"
    assert any("AI-generated" in w and "image" in w for w in res.warnings)


def test_image_only_change_keeps_the_source_changed_even_when_the_text_is_cosmetic(compare, mocker):
    mocker.patch("digest.spec_monitor.textdiff.is_cosmetic", return_value=True)
    compare.return_value = VisionDiff("Limit changed", ["limit 10 => 20"], "high")
    mon, _ = make_monitor({"shot.png": versions((1, 1), (2, 8))}, {("shot.png", 1): PNG, ("shot.png", 2): JPEG})
    assert resolve(mon).classification == Classification.CHANGED


def test_unchanged_image_is_not_compared_and_cosmetic_text_stays_cosmetic(compare, mocker):
    mocker.patch("digest.spec_monitor.textdiff.is_cosmetic", return_value=True)
    mon, client = make_monitor({"shot.png": versions((1, 1), (2, 2))}, {})
    res = resolve(mon)
    assert res.classification == Classification.COSMETIC_ONLY
    assert not compare.called and not client.download_attachment.called


def test_identical_bytes_are_not_sent_to_the_model(compare, mocker):
    mocker.patch("digest.spec_monitor.textdiff.is_cosmetic", return_value=True)
    mon, _ = make_monitor({"shot.png": versions((1, 1), (2, 8))}, {("shot.png", 1): PNG, ("shot.png", 2): PNG})
    res = resolve(mon)
    assert res.classification == Classification.COSMETIC_ONLY and not compare.called


def test_image_added_or_removed_is_left_to_the_text_diff(compare):
    bodies = {3: "<p>old</p>", 5: "<p>new</p>" + img("new.png")}
    mon, client = make_monitor({"new.png": versions((1, 4))}, {}, bodies=bodies)
    resolve(mon)
    assert not client.attachment_versions.called and not compare.called


def test_without_vision_a_changed_image_is_reported_as_not_compared(compare):
    mon, _ = make_monitor({"shot.png": versions((1, 1), (2, 8))}, {}, vision_cfg=None)
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    assert any("image 'shot.png' changed, not compared (visual comparison is not enabled)" in n
               for n in res.diagram_notes)
    assert not compare.called


def test_vision_error_becomes_a_note(compare):
    compare.side_effect = VisionError("endpoint down")
    mon, _ = make_monitor({"shot.png": versions((1, 1), (2, 8))}, {("shot.png", 1): PNG, ("shot.png", 2): JPEG})
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    assert any("image 'shot.png' changed, not compared (visual comparison failed: endpoint down)" in n
               for n in res.diagram_notes)


def test_unavailable_vision_is_not_tried_for_the_next_image(compare):
    compare.side_effect = vision.VisionUnavailable("m1 cannot be used for images")
    bodies = {3: img("a.png") + img("b.png") + "<p>o</p>", 5: img("a.png") + img("b.png") + "<p>n</p>"}
    mon, _ = make_monitor(
        {n: versions((1, 1), (2, 8)) for n in ("a.png", "b.png")},
        {(n, v): PNG for n in ("a.png", "b.png") for v in (1, 2)} | {("a.png", 2): JPEG, ("b.png", 2): JPEG},
        bodies=bodies)
    res = resolve(mon)
    assert compare.call_count == 1
    assert sum("not compared" in n for n in res.diagram_notes) == 2


def test_download_failure_becomes_a_note(compare):
    mon, client = make_monitor({"shot.png": versions((1, 1), (2, 8))}, {})
    client.download_attachment.side_effect = RequestFailed("u", 3, "HTTP 500")
    res = resolve(mon)
    assert res.classification == Classification.CHANGED
    assert any("image 'shot.png' changed, not compared (download failed: HTTP 500)" in n for n in res.diagram_notes)


def test_version_listing_failure_becomes_a_note_without_failing_the_source(compare):
    mon, _ = make_monitor(RequestFailed("u", 3, "HTTP 500"), {})
    res = resolve(mon)
    assert res.classification == Classification.CHANGED   # the text did change
    assert any("image 'shot.png' not compared (attachment versions unavailable: HTTP 500)" in n
               for n in res.diagram_notes)


def test_missing_attachment_is_noted(compare):
    mon, _ = make_monitor({}, {})
    res = resolve(mon)
    assert any("image 'shot.png' not compared (attachment not found)" in n for n in res.diagram_notes)


def test_no_attachment_version_at_the_old_page_version_is_noted(compare):
    mon, _ = make_monitor({"shot.png": versions((1, 9))}, {})
    res = resolve(mon)
    assert any("no attachment version at the time of page version 3" in n for n in res.diagram_notes)


def test_images_beyond_the_limit_are_reported_not_compared(compare):
    compare.return_value = VisionDiff("s", ["c"], "high")
    names = [f"i{n}.png" for n in range(MAX_IMAGE_COMPARISONS + 2)]
    bodies = {3: "".join(map(img, names)) + "<p>o</p>", 5: "".join(map(img, names)) + "<p>n</p>"}
    files = {}
    for n in names:
        files[(n, 1)], files[(n, 2)] = PNG, JPEG
    mon, _ = make_monitor({n: versions((1, 1), (2, 8)) for n in names}, files, bodies=bodies)
    res = resolve(mon)
    assert compare.call_count == MAX_IMAGE_COMPARISONS
    assert sum("limit of" in n for n in res.diagram_notes) == 2


def test_drawio_previews_in_the_body_are_not_compared_as_plain_images(compare, mocker):
    mocker.patch("digest.spec_monitor.confluence_monitor.drawio.diff_diagrams",
                 return_value=MagicMock(changes=[], layout_only=True, warnings=[]))
    bodies = {3: img("arch.png") + "<p>o</p>", 5: img("arch.png") + "<p>n</p>"}
    mon, client = make_monitor({"arch.png": versions((1, 1), (2, 8))}, {}, bodies=bodies)
    mocker.patch("digest.spec_monitor.textdiff.drawio_revisions", return_value={"arch": 1})
    resolve(mon)
    assert not client.attachment_versions.called


def test_other_and_external_images_are_listed_as_not_monitored(compare):
    ext = '<ac:image><ri:url ri:value="https://example.com/p.png" /></ac:image>'
    mon, _ = make_monitor({}, {}, bodies={3: ext + "<p>o</p>", 5: ext + "<p>n</p>"})
    res = resolve(mon)
    assert any("1 image(s) from other pages or external URLs are not monitored" in w for w in res.warnings)


# --- client ------------------------------------------------------------------

def test_client_lists_attachment_versions_oldest_first(mocker):
    client = ConfluenceClient.__new__(ConfluenceClient)
    client._api = "https://c/wiki/rest/api"
    client._atlassian = MagicMock(url="https://c")
    client._get = MagicMock(return_value=MagicMock(
        status_code=200, ok=True, json=lambda: {"results": [{"id": "att9"}]}))
    client._paginate = MagicMock(return_value=[
        {"number": 2, "when": "2026-11-08T10:00:00.000Z"}, {"number": 1, "when": "2026-11-01T10:00:00.000Z"}])
    points = client.attachment_versions("100", "shot.png")
    assert [p.version for p in points] == ["1", "2"] and points[1].when is not None
    assert client._paginate.call_args.args[0] == "https://c/wiki/rest/api/content/att9/version"


def test_client_returns_none_for_an_unknown_attachment():
    client = ConfluenceClient.__new__(ConfluenceClient)
    client._api = "https://c/wiki/rest/api"
    client._get = MagicMock(return_value=MagicMock(status_code=200, ok=True, json=lambda: {"results": []}))
    assert client.attachment_versions("100", "nope.png") is None
