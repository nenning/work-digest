"""Monitor for Confluence page sources: pinned version vs latest, diffed."""
import logging
import re
from datetime import date
from typing import List, Optional

from digest.config import VisionLLMConfig
from digest.spec_monitor import drawio, textdiff, vision
from digest.spec_monitor.cache import SpecCache
from digest.spec_monitor.confluence_client import (
    ConfluenceClient, NoStorageBody, parse_when, pick_version_at, pick_version_before,
)
from digest.spec_monitor.http import RequestFailed
from digest.spec_monitor.models import (
    Classification, SourceKind, SourceRef, SourceResult, SourceStep, SpecInfo, VersionPoint,
)

log = logging.getLogger(__name__)

MAX_STEPS = 12
MAX_IMAGE_COMPARISONS = 5   # vision calls for changed plain images per source and run

_SOURCES_TABLE = re.compile(
    r"<h[1-6]\b[^>]*>(?:(?!</h[1-6]>).)*?Quelle(?:(?!</h[1-6]>).)*</h[1-6]>"
    r"(?:(?!<h[1-6]\b).)*?(<table\b.*?</table>)",
    re.S | re.I,
)


def _sources_table(storage: str) -> Optional[str]:
    m = _SOURCES_TABLE.search(storage or "")
    return m.group(1) if m else None


class _Failure(Exception):
    """Expected, already worded problem for one source."""


def _error(ref: SourceRef, message: str, attempts: int = 1,
           old: Optional[VersionPoint] = None, new: Optional[VersionPoint] = None) -> SourceResult:
    return SourceResult(ref=ref, classification=Classification.ERROR, old=old, new=new,
                        errors=[message], attempts=attempts)


class ConfluenceMonitor:
    def __init__(self, client: ConfluenceClient, vision: Optional[VisionLLMConfig] = None,
                 language: str = "de", cache: Optional[SpecCache] = None):
        self._client = client
        self._vision = vision
        self._language = language
        self._cache = cache
        self._vision_off: Optional[str] = None

    def handles(self, ref: SourceRef) -> bool:
        return ref.kind == SourceKind.CONFLUENCE

    def resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        label = ref.title or ref.name
        try:
            return self._resolve(ref, spec)
        except _Failure as exc:
            return _error(ref, str(exc))
        except RequestFailed as exc:
            return _error(ref, f"{label}: {exc.last_error} (after {exc.attempts} attempt(s))", attempts=exc.attempts)
        except LookupError as exc:
            return _error(ref, f"{label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - monitors must never raise
            log.exception("confluence monitor failed for %s", label)
            return _error(ref, f"{label}: unexpected error {type(exc).__name__}: {exc}")

    def _resolve(self, ref: SourceRef, spec: SpecInfo) -> SourceResult:
        client = self._client
        page_id = ref.page_id
        if not page_id:
            if not ref.title:
                raise _Failure(f"{ref.name}: no page title or id to look up")
            page_id = client.find_page_id(ref.title, ref.space_key)
            if not page_id:
                where = f" in space {ref.space_key}" if ref.space_key else ""
                raise _Failure(f"page '{ref.title}' not found{where}")

        live = client.get_page(page_id, with_body=False)
        live_version = (live.get("version") or {})
        new_number = int(live_version["number"])
        new = VersionPoint(version=str(new_number), when=parse_when(live_version.get("when")),
                           url=client.page_url(page_id, new_number))

        old = self._old_version(ref, spec, page_id, new_number, new)
        if old.version == new.version:
            return SourceResult(ref=ref, classification=Classification.UNCHANGED, old=old, new=new)

        content_type = live.get("type") or "page"
        if content_type != "page":
            return self._not_compared(ref, page_id, old, new, content_type)
        try:
            old_body = client.get_body(page_id, int(old.version))
            new_body = client.get_body(page_id, new_number)
        except NoStorageBody:
            return self._not_compared(ref, page_id, old, new, content_type)
        old_text = textdiff.storage_to_text(old_body)
        new_text = textdiff.storage_to_text(new_body)
        diff = textdiff.diff_text(old_text, new_text)

        result = SourceResult(ref=ref, classification=Classification.CHANGED, old=old, new=new,
                              diff_url=client.diff_url(page_id, int(old.version), new_number),
                              old_text=old_text, new_text=new_text)
        diagram_changed = self._compare_diagrams(page_id, old_body, new_body, result)
        diagram_changed = self._compare_images(page_id, old, old_body, new_body, result) or diagram_changed
        self._links(spec, old_body, new_body, ref, result)

        if textdiff.is_cosmetic(diff) and not diagram_changed:
            result.classification = Classification.COSMETIC_ONLY
            result.diff_text = ""
        else:
            result.diff_text = diff
            self._fill_steps(page_id, old, new, old_text, new_body, result)
        return result

    def _not_compared(self, ref: SourceRef, page_id: str, old: VersionPoint, new: VersionPoint,
                      content_type: str) -> SourceResult:
        # databases, whiteboards, embeds and folders have no body.storage: only the version number is known
        return SourceResult(
            ref=ref, classification=Classification.CHANGED, old=old, new=new,
            diff_url=self._client.diff_url(page_id, int(old.version), int(new.version)),
            warnings=[f"content type {content_type} has no storage body; "
                      f"versions {old.version} -> {new.version} not compared"],
        )

    def _fill_steps(self, page_id: str, old: VersionPoint, new: VersionPoint, old_text: str,
                    new_body: str, result: SourceResult) -> None:
        first, last = int(old.version), int(new.version)
        count = last - first
        if count <= 1:
            return
        if count > MAX_STEPS:
            result.warnings.append(f"history of {count} versions not itemised (cap)")
            return
        try:
            whens = {int(v.version): v.when for v in self._client.list_versions(page_id)}
        except (RequestFailed, LookupError) as exc:
            whens = {}
            result.warnings.append(f"version dates of the history are unavailable ({exc})")
        steps: List[SourceStep] = []
        prev_text = old_text
        for number in range(first + 1, last + 1):
            if number == last:
                body = new_body
            else:
                try:
                    body = self._client.get_body(page_id, number)
                except (RequestFailed, LookupError):
                    result.warnings.append(f"history of {count} versions not itemised (version {number} unreadable)")
                    return
            text = textdiff.storage_to_text(body)
            step_diff = textdiff.diff_text(prev_text, text)
            prev_text = text
            if textdiff.is_cosmetic(step_diff):
                continue
            when = whens.get(number) or (new.when if number == last else None)
            steps.append(SourceStep(version=str(number), when=when, diff_text=step_diff))
        result.steps = steps

    def _old_version(self, ref: SourceRef, spec: SpecInfo, page_id: str, new_number: int,
                     new: VersionPoint) -> VersionPoint:
        client = self._client
        if ref.baseline_version:
            stated = ref.baseline_version.strip().lstrip("vV")
            if stated.isdigit() and int(stated) == new_number:
                return VersionPoint(version=new.version, when=new.when, url=new.url)
            versions = client.list_versions(page_id)
            for v in versions:
                if stated.isdigit() and int(v.version) == int(stated):
                    return v
            raise _Failure(
                f"{ref.title or ref.name}: stated version '{ref.baseline_version}' not found in the page history "
                f"(versions 1-{versions[-1].version if versions else '?'})"
            )

        day, reason = self._fallback_day(ref, spec)
        if day is None:
            raise _Failure(f"{ref.title or ref.name}: no version, date, Stand date or spec creation date to infer a baseline from")
        point = pick_version_at(client.list_versions(page_id), day)
        if point is None:
            raise _Failure(f"{ref.title or ref.name}: no version at or before {day.isoformat()} ({reason}); page was created later")
        return VersionPoint(version=point.version, when=point.when, url=point.url,
                            inferred=True, inferred_reason=reason)

    @staticmethod
    def _fallback_day(ref: SourceRef, spec: SpecInfo):
        if ref.baseline_date:
            return ref.baseline_date, f"from date {ref.baseline_date.isoformat()}"
        if spec.stand_date:
            return spec.stand_date, f"from Stand date {spec.stand_date.isoformat()} of the spec"
        if spec.created:
            day: date = spec.created.date()
            return day, f"from creation date {day.isoformat()} of the spec"
        return None, ""

    def _links(self, spec: SpecInfo, old_body: str, new_body: str, ref: SourceRef, result: SourceResult) -> None:
        known = {t.casefold() for t in textdiff.extract_page_links(_sources_table(spec.body_storage) or spec.body_storage)}
        known.update(t.casefold() for t in spec.ancestor_titles)
        if ref.title:
            known.add(ref.title.casefold())
        result.added_links = [t for t in textdiff.added_links(old_body, new_body) if t.casefold() not in known]
        added = {t.casefold() for t in result.added_links}
        result.other_links = [
            t for t in textdiff.extract_page_links(new_body)
            if t.casefold() not in known and t.casefold() not in added
        ]

    def _compare_diagrams(self, page_id: str, old_body: str, new_body: str, result: SourceResult) -> bool:
        old_revs = textdiff.drawio_revisions(old_body)
        new_revs = textdiff.drawio_revisions(new_body)
        changed = False
        for name, new_rev in new_revs.items():
            if name not in old_revs:
                result.diagram_notes.append(f"diagram '{name}' added, not compared")
                changed = True
            elif old_revs[name] != new_rev:
                changed |= self._compare_one(page_id, name, old_revs[name], new_rev, result)
        for name in old_revs:
            if name not in new_revs:
                result.diagram_notes.append(f"diagram '{name}' removed")
                changed = True
        return changed

    def _compare_one(self, page_id: str, name: str, old_rev: int, new_rev: int, result: SourceResult) -> bool:
        def not_compared(reason: str) -> bool:
            if self._vision is None:
                result.diagram_notes.append(f"diagram changed, not compared: {name} ({reason})"
                                            " (visual comparison is not enabled)")
                return True
            changed, failure = self._compare_visually(page_id, name, old_rev, new_rev, result)
            if failure:
                result.diagram_notes.append(f"diagram changed, not compared: {name} ({reason}); {failure}")
                return True
            return changed

        try:
            old_xml = self._client.download_attachment(page_id, name, old_rev)
            new_xml = self._client.download_attachment(page_id, name, new_rev)
        except RequestFailed as exc:
            return not_compared(f"download failed: {exc.last_error}")
        except LookupError as exc:
            return not_compared(str(exc))
        if old_xml is None or new_xml is None:
            return not_compared("attachment revision not available")
        try:
            diff = drawio.diff_diagrams(old_xml.decode("utf-8", errors="replace"),
                                        new_xml.decode("utf-8", errors="replace"))
        except ValueError as exc:
            return not_compared(str(exc))
        result.warnings.extend(diff.warnings)
        if diff.layout_only or not diff.changes:
            return False
        result.diagram_notes.extend(f"diagram '{name}': {c}" for c in diff.changes)
        return True

    def _compare_visually(self, page_id: str, name: str, old_rev: int, new_rev: int,
                          result: SourceResult):
        """Returns (changed, failure); failure is a worded reason when no visual comparison happened."""
        if self._vision_off:
            return False, f"visual comparison skipped: {self._vision_off}"
        old_png = new_png = None
        try:
            for candidate in (f"{name}.drawio.png", f"{name}.png"):
                old_png = self._client.download_attachment(page_id, candidate, old_rev)
                new_png = self._client.download_attachment(page_id, candidate, new_rev)
                if old_png is not None and new_png is not None:
                    break
        except RequestFailed as exc:
            return False, f"visual comparison failed: PNG preview download failed: {exc.last_error}"
        except LookupError as exc:
            return False, f"visual comparison failed: {exc}"
        if old_png is None or new_png is None:
            return False, "no PNG preview available for a visual comparison"
        return self._run_vision(page_id, name, old_png, new_png, result, "diagram")

    def _run_vision(self, page_id: str, name: str, old_img: bytes, new_img: bytes, result: SourceResult,
                    kind: str):
        """Returns (changed, failure) like _compare_visually; kind is "diagram" or "image"."""
        try:
            diff = vision.compare_images(
                old_img, new_img, diagram_name=name, context=f"Confluence page {page_id}",
                vision=self._vision, language=self._language, cache=self._cache, kind=kind,
            )
        except vision.VisionUnavailable as exc:
            hint = ("; set llm.vision_model (or put an image-capable model first in "
                    "llm.heavy_models) to compare diagrams visually")
            self._vision_off = f"{exc} (not tried again in this run{hint})"
            return False, f"visual comparison failed: {self._vision_off}"
        except vision.VisionError as exc:
            return False, f"visual comparison failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - vision problems must never fail the source
            return False, f"visual comparison failed: {type(exc).__name__}: {exc}"
        detail = "; ".join([diff.summary] + diff.changes)
        result.diagram_notes.append(
            f"{kind} '{name}' compared visually (AI, {diff.confidence} confidence): {detail}")
        warning = f"visual {kind} comparison is AI-generated and may be incomplete or wrong; verify in the {kind}"
        if warning not in result.warnings:
            result.warnings.append(warning)
        if not diff.changes and diff.confidence == "low":
            result.warnings.append(
                f"{kind} '{name}': visual comparison found no difference but with low confidence; verify in the {kind}"
            )
        return bool(diff.changes), None

    def _changed_images(self, page_id: str, old: VersionPoint, old_body: str, new_body: str,
                        result: SourceResult):
        """Plain images attached to the page and present in both versions whose attachment changed:
        [(name, old attachment version, new attachment version)]. Compares the attachment version that
        was current at the old page version with the latest one."""
        new_names, other = textdiff.image_attachments(new_body)
        old_names, _ = textdiff.image_attachments(old_body)
        if other:
            result.warnings.append(
                f"{other} image(s) from other pages or external URLs are not monitored for changes")
        diagram_names = set(textdiff.drawio_revisions(old_body)) | set(textdiff.drawio_revisions(new_body))
        previews = {f"{n}{ext}" for n in diagram_names for ext in (".png", ".drawio.png")}
        found = []
        for name in (n for n in new_names if n in old_names and n not in previews):
            try:
                versions = self._client.attachment_versions(page_id, name)
            except (RequestFailed, LookupError) as exc:
                reason = getattr(exc, "last_error", None) or exc
                result.diagram_notes.append(f"image '{name}' not compared (attachment versions unavailable: {reason})")
                continue
            if not versions:
                result.diagram_notes.append(f"image '{name}' not compared (attachment not found)")
                continue
            old_v = pick_version_before(versions, old.when) if old.when else None
            if old_v is None:
                result.diagram_notes.append(
                    f"image '{name}' not compared (no attachment version at the time of page version {old.version})")
                continue
            if int(versions[-1].version) > int(old_v.version):
                found.append((name, int(old_v.version), int(versions[-1].version)))
        return found

    def _compare_images(self, page_id: str, old: VersionPoint, old_body: str, new_body: str,
                        result: SourceResult) -> bool:
        """Visual comparison of every changed plain image. True when one changed or could not be compared."""
        changed = False
        for index, (name, old_ver, new_ver) in enumerate(self._changed_images(page_id, old, old_body, new_body, result)):
            def not_compared(reason: str) -> bool:
                result.diagram_notes.append(f"image '{name}' changed, not compared ({reason})")
                return True

            if index >= MAX_IMAGE_COMPARISONS:
                changed |= not_compared(f"limit of {MAX_IMAGE_COMPARISONS} visually compared images per source")
            elif self._vision is None:
                changed |= not_compared("visual comparison is not enabled")
            elif self._vision_off:
                changed |= not_compared(f"visual comparison skipped: {self._vision_off}")
            else:
                try:
                    old_img = self._client.download_attachment(page_id, name, old_ver)
                    new_img = self._client.download_attachment(page_id, name, new_ver)
                except RequestFailed as exc:
                    changed |= not_compared(f"download failed: {exc.last_error}")
                    continue
                except LookupError as exc:
                    changed |= not_compared(str(exc))
                    continue
                if old_img is None or new_img is None:
                    changed |= not_compared("attachment version not available")
                elif old_img != new_img:   # identical bytes: only the upload is new
                    differs, failure = self._run_vision(page_id, name, old_img, new_img, result, "image")
                    changed |= not_compared(failure) if failure else differs
        return changed
