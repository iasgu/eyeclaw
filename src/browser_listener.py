from __future__ import annotations

import base64
import binascii
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, BinaryIO, Literal
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


BrowserEventType = Literal[
    "navigation",
    "history",
    "tab_activated",
    "tab_updated",
    "page_loaded",
    "click",
    "input",
    "change",
    "scroll",
    "visibility",
    "focus",
]

BrowserEventSource = Literal["extension_background", "extension_content"]

MAX_STRING_LENGTH = 500
MAX_DETAILS_ITEMS = 24
LISTENER_ARTIFACT_DIR = Path("artifacts/listener_frames")
SESSION_RECORDINGS_DIR = Path("artifacts/session_recordings")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trim_text(value: str | None, *, limit: int = MAX_STRING_LENGTH) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3]}..."


class BrowserEventIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_type: BrowserEventType
    source: BrowserEventSource = "extension_content"
    page_url: str | None = None
    page_title: str | None = None
    tab_id: int | None = None
    window_id: int | None = None
    frame_id: int | None = None
    target_text: str | None = None
    target_selector: str | None = None
    target_tag: str | None = None
    target_type: str | None = None
    input_value: str | None = None
    scroll_x: int | None = None
    scroll_y: int | None = None
    delta_x: int | None = None
    delta_y: int | None = None
    client_timestamp_ms: int | None = None
    key_candidate: bool | None = None
    screenshot_data_url: str | None = None
    screenshot_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "page_url",
        "page_title",
        "target_text",
        "target_selector",
        "target_tag",
        "target_type",
        "input_value",
        "screenshot_reason",
        mode="before",
    )
    @classmethod
    def _trim_strings(cls, value: str | None) -> str | None:
        return trim_text(value)

    @field_validator("screenshot_data_url", mode="before")
    @classmethod
    def _keep_reasonable_screenshot_size(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text.startswith("data:image/"):
            return None
        if len(text) > 12_000_000:
            return None
        return text

    @field_validator("details", mode="before")
    @classmethod
    def _trim_details(cls, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        trimmed: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_DETAILS_ITEMS:
                break
            key_text = trim_text(str(key), limit=64)
            if not key_text:
                continue
            if isinstance(item, str):
                trimmed[key_text] = trim_text(item, limit=240)
            elif isinstance(item, (int, float, bool)) or item is None:
                trimmed[key_text] = item
            else:
                trimmed[key_text] = trim_text(str(item), limit=240)
        return trimmed


class BrowserEventBatchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_name: str = "eyeclaw-listener"
    browser_name: str | None = None
    session_id: str = Field(default_factory=lambda: uuid4().hex)
    events: list[BrowserEventIn]

    @field_validator("client_name", "browser_name", "session_id", mode="before")
    @classmethod
    def _trim_batch_strings(cls, value: str | None) -> str | None:
        return trim_text(value, limit=120)


class BrowserEvent(BaseModel):
    event_id: str
    client_name: str
    browser_name: str | None = None
    session_id: str
    received_at_iso: str
    event_type: BrowserEventType
    source: BrowserEventSource
    page_url: str | None = None
    page_title: str | None = None
    tab_id: int | None = None
    window_id: int | None = None
    frame_id: int | None = None
    target_text: str | None = None
    target_selector: str | None = None
    target_tag: str | None = None
    target_type: str | None = None
    input_value: str | None = None
    scroll_x: int | None = None
    scroll_y: int | None = None
    delta_x: int | None = None
    delta_y: int | None = None
    client_timestamp_ms: int | None = None
    is_key_candidate: bool
    screenshot_path: str | None = None
    screenshot_reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class ListenerGuidedFrame:
    timestamp_second: float
    hint: str
    event_id: str


@dataclass(frozen=True)
class SessionRecording:
    session_id: str
    recording_path: str
    mime_type: str
    tab_id: int | None
    started_at_ms: int | None
    ended_at_ms: int | None
    saved_at_iso: str


class BrowserEventStore:
    def __init__(self, max_events: int = 800, artifact_root: Path | None = None) -> None:
        self._events: deque[BrowserEvent] = deque(maxlen=max_events)
        self._lock = Lock()
        self._received_count = 0
        self._artifact_root = artifact_root or LISTENER_ARTIFACT_DIR
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._recordings: dict[str, SessionRecording] = {}

    def ingest(self, batch: BrowserEventBatchIn) -> list[BrowserEvent]:
        received_at_iso = utc_now_iso()
        accepted: list[BrowserEvent] = []
        for raw_event in batch.events:
            event_id = uuid4().hex
            screenshot_path = self._persist_screenshot(
                session_id=batch.session_id,
                event_id=event_id,
                screenshot_data_url=raw_event.screenshot_data_url,
            )
            accepted.append(
                BrowserEvent(
                    event_id=event_id,
                    client_name=batch.client_name,
                    browser_name=batch.browser_name,
                    session_id=batch.session_id,
                    received_at_iso=received_at_iso,
                    event_type=raw_event.event_type,
                    source=raw_event.source,
                    page_url=raw_event.page_url,
                    page_title=raw_event.page_title,
                    tab_id=raw_event.tab_id,
                    window_id=raw_event.window_id,
                    frame_id=raw_event.frame_id,
                    target_text=raw_event.target_text,
                    target_selector=raw_event.target_selector,
                    target_tag=raw_event.target_tag,
                    target_type=raw_event.target_type,
                    input_value=raw_event.input_value,
                    scroll_x=raw_event.scroll_x,
                    scroll_y=raw_event.scroll_y,
                    delta_x=raw_event.delta_x,
                    delta_y=raw_event.delta_y,
                    client_timestamp_ms=raw_event.client_timestamp_ms,
                    is_key_candidate=infer_key_candidate(raw_event),
                    screenshot_path=str(screenshot_path) if screenshot_path else None,
                    screenshot_reason=raw_event.screenshot_reason,
                    details=raw_event.details,
                )
            )

        with self._lock:
            self._events.extend(accepted)
            self._received_count += len(accepted)

        return accepted

    def clear(self) -> int:
        with self._lock:
            snapshot = list(self._events)
            cleared = len(snapshot)
            self._events.clear()
            recordings = list(self._recordings.values())
            self._recordings.clear()
        for event in snapshot:
            if event.screenshot_path:
                try:
                    Path(event.screenshot_path).unlink(missing_ok=True)
                except OSError:
                    pass
        for recording in recordings:
            try:
                Path(recording.recording_path).unlink(missing_ok=True)
            except OSError:
                pass
        self._remove_empty_session_dirs()
        return cleared

    def list_events(
        self,
        limit: int = 50,
        *,
        session_id: str | None = None,
        only_with_screenshots: bool = False,
    ) -> list[BrowserEvent]:
        capped_limit = max(1, min(limit, 200))
        snapshot = self._filtered_snapshot(session_id=session_id, only_with_screenshots=only_with_screenshots)
        selected = snapshot[max(0, len(snapshot) - capped_limit) :]
        selected.reverse()
        return selected

    def latest_session_id(self) -> str | None:
        with self._lock:
            if not self._events:
                if not self._recordings:
                    return None
                return list(self._recordings.keys())[-1]
            return self._events[-1].session_id

    def set_session_recording(
        self,
        session_id: str,
        *,
        recording_path: str,
        mime_type: str,
        tab_id: int | None,
        started_at_ms: int | None,
        ended_at_ms: int | None,
    ) -> SessionRecording:
        recording = SessionRecording(
            session_id=session_id,
            recording_path=recording_path,
            mime_type=mime_type,
            tab_id=tab_id,
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            saved_at_iso=utc_now_iso(),
        )
        with self._lock:
            self._recordings[session_id] = recording
        return recording

    def get_session_recording(self, session_id: str | None) -> SessionRecording | None:
        if not session_id:
            return None
        with self._lock:
            return self._recordings.get(session_id)

    def session_summary(self, session_id: str) -> dict[str, Any]:
        events = self._filtered_snapshot(session_id=session_id)
        recording = self.get_session_recording(session_id)
        screenshot_count = sum(1 for event in events if event.screenshot_path)
        return {
            "session_id": session_id,
            "event_count": len(events),
            "screenshot_count": screenshot_count,
            "recording_path": recording.recording_path if recording else None,
            "recording_mime_type": recording.mime_type if recording else None,
            "recording_started_at_ms": recording.started_at_ms if recording else None,
            "recording_ended_at_ms": recording.ended_at_ms if recording else None,
        }

    def select_analysis_candidates(self, session_id: str | None = None, limit: int = 8) -> tuple[str | None, list[BrowserEvent]]:
        effective_session_id = session_id or self.latest_session_id()
        if not effective_session_id:
            return None, []

        snapshot = self._filtered_snapshot(session_id=effective_session_id, only_with_screenshots=True)
        candidates = [event for event in snapshot if event.is_key_candidate and event.screenshot_path and Path(event.screenshot_path).exists()]

        deduped: list[BrowserEvent] = []
        for event in candidates:
            if deduped and _events_are_redundant(deduped[-1], event):
                continue
            deduped.append(event)

        capped_limit = max(1, min(limit, 20))
        return effective_session_id, deduped[-capped_limit:]

    def session_events(self, session_id: str) -> list[BrowserEvent]:
        return self._filtered_snapshot(session_id=session_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            snapshot = list(self._events)
            received_count = self._received_count
            recordings = dict(self._recordings)

        latest = snapshot[-1] if snapshot else None
        event_counts = Counter(event.event_type for event in snapshot)
        session_counts = Counter(event.session_id for event in snapshot)
        screenshot_count = sum(1 for event in snapshot if event.screenshot_path)
        latest_recording_session_id = list(recordings.keys())[-1] if recordings else None

        return {
            "buffered_events": len(snapshot),
            "received_count": received_count,
            "last_event_at_iso": latest.received_at_iso if latest else None,
            "last_event_type": latest.event_type if latest else None,
            "latest_session_id": latest.session_id if latest else None,
            "active_session_ids": list(session_counts.keys())[-5:],
            "event_type_counts": dict(event_counts),
            "screenshot_event_count": screenshot_count,
            "recorded_session_count": len(recordings),
            "latest_recording_session_id": latest_recording_session_id,
        }

    def _filtered_snapshot(
        self,
        *,
        session_id: str | None = None,
        only_with_screenshots: bool = False,
    ) -> list[BrowserEvent]:
        with self._lock:
            snapshot = list(self._events)
        if session_id:
            snapshot = [event for event in snapshot if event.session_id == session_id]
        if only_with_screenshots:
            snapshot = [event for event in snapshot if event.screenshot_path]
        return snapshot

    def _persist_screenshot(self, session_id: str, event_id: str, screenshot_data_url: str | None) -> Path | None:
        if not screenshot_data_url:
            return None
        try:
            header, encoded = screenshot_data_url.split(",", 1)
        except ValueError:
            return None
        if ";base64" not in header:
            return None
        extension = "png"
        if header.startswith("data:image/"):
            extension = header[len("data:image/") :].split(";", 1)[0] or "png"
        if extension == "jpeg":
            extension = "jpg"
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None

        session_dir = self._artifact_root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        target_path = session_dir / f"{event_id}.{extension}"
        target_path.write_bytes(image_bytes)
        return target_path

    def _remove_empty_session_dirs(self) -> None:
        if not self._artifact_root.exists():
            return
        for child in self._artifact_root.iterdir():
            if child.is_dir():
                try:
                    child.rmdir()
                except OSError:
                    continue


def infer_key_candidate(event: BrowserEventIn) -> bool:
    if event.key_candidate is not None:
        return event.key_candidate

    if event.event_type in {
        "navigation",
        "history",
        "tab_activated",
        "tab_updated",
        "page_loaded",
        "click",
        "change",
    }:
        return True
    if event.event_type == "input":
        return bool(event.input_value)
    if event.event_type == "scroll":
        return abs(event.delta_y or 0) >= 400 or abs(event.scroll_y or 0) >= 600
    return False


def summarize_browser_event(event: BrowserEvent) -> str:
    parts = [event.event_type]
    if event.target_text:
        parts.append(f"target={event.target_text}")
    elif event.target_tag:
        parts.append(f"target={event.target_tag}")

    if event.input_value:
        parts.append(f"value={event.input_value}")

    if event.page_title:
        parts.append(f"title={event.page_title}")

    if event.page_url:
        parsed = urlparse(event.page_url)
        path = parsed.path or "/"
        parts.append(f"url={parsed.netloc}{path}")

    if event.event_type == "scroll" and event.scroll_y is not None:
        parts.append(f"scroll_y={event.scroll_y}")

    return " | ".join(parts)


def choose_site_url(events: list[BrowserEvent], fallback_site_url: str) -> str:
    for event in events:
        if event.page_url:
            return event.page_url
    return fallback_site_url


def save_session_recording(
    session_id: str,
    filename: str,
    stream: BinaryIO,
) -> Path:
    target_dir = SESSION_RECORDINGS_DIR / session_id
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix or ".webm"
    target_path = target_dir / f"session_recording{suffix}"
    target_path.write_bytes(stream.read())
    return target_path


def plan_listener_guided_frames(
    events: list[BrowserEvent],
    start_second: float,
    end_second: float,
    max_frames: int,
) -> list[ListenerGuidedFrame]:
    if max_frames <= 0 or end_second < start_second:
        return []

    key_events = [event for event in events if event.is_key_candidate]
    if not key_events:
        return []

    segment_duration = max(0.0, end_second - start_second)
    if segment_duration == 0:
        chosen = key_events[:1]
        return [
            ListenerGuidedFrame(
                timestamp_second=round(start_second, 2),
                hint=f"{summarize_browser_event(chosen[0])} | relative=center",
                event_id=chosen[0].event_id,
            )
        ]

    normalized_positions = _normalized_event_positions(key_events)
    neighbor_window = min(max(segment_duration / 30.0, 0.45), 1.2)
    min_gap = min(max(segment_duration / 80.0, 0.2), 0.6)

    center_candidates = [
        _frame_candidate(
            event=event,
            second=_clamp_round(start_second + position * segment_duration, start_second, end_second),
            relative="center",
        )
        for event, position in zip(key_events, normalized_positions)
    ]
    chosen = _take_spaced_candidates(center_candidates, max_frames=max_frames, min_gap=min_gap)

    if len(chosen) < max_frames:
        after_candidates = [
            _frame_candidate(
                event=event,
                second=_clamp_round(center.timestamp_second + neighbor_window, start_second, end_second),
                relative="after",
            )
            for event, center in zip(key_events, center_candidates)
            if event.event_type in {"navigation", "history", "tab_activated", "tab_updated", "page_loaded", "click", "change"}
        ]
        chosen = _merge_spaced_candidates(chosen, after_candidates, max_frames=max_frames, min_gap=min_gap)

    if len(chosen) < max_frames:
        before_candidates = [
            _frame_candidate(
                event=event,
                second=_clamp_round(center.timestamp_second - neighbor_window, start_second, end_second),
                relative="before",
            )
            for event, center in zip(key_events, center_candidates)
            if event.event_type in {"navigation", "history", "tab_activated", "tab_updated", "page_loaded", "click", "change"}
        ]
        chosen = _merge_spaced_candidates(chosen, before_candidates, max_frames=max_frames, min_gap=min_gap)

    if len(chosen) < max_frames:
        fillers = _gap_fill_candidates(chosen, max_frames=max_frames, start_second=start_second, end_second=end_second)
        chosen = _merge_spaced_candidates(chosen, fillers, max_frames=max_frames, min_gap=min_gap)

    chosen.sort(key=lambda item: item.timestamp_second)
    return chosen[:max_frames]


def _events_are_redundant(previous: BrowserEvent, current: BrowserEvent) -> bool:
    return (
        previous.event_type == current.event_type
        and previous.page_url == current.page_url
        and previous.target_text == current.target_text
        and previous.target_selector == current.target_selector
    )


def _normalized_event_positions(events: list[BrowserEvent]) -> list[float]:
    timestamps = [event.client_timestamp_ms for event in events if event.client_timestamp_ms is not None]
    if len(timestamps) >= 2 and max(timestamps) > min(timestamps):
        min_ts = min(timestamps)
        max_ts = max(timestamps)
        span = max_ts - min_ts
        positions: list[float] = []
        for index, event in enumerate(events):
            if event.client_timestamp_ms is None:
                fallback = index / max(len(events) - 1, 1)
                positions.append(fallback)
            else:
                positions.append((event.client_timestamp_ms - min_ts) / span)
        return positions

    if len(events) == 1:
        return [0.5]

    return [index / (len(events) - 1) for index, _ in enumerate(events)]


def _clamp_round(value: float, start_second: float, end_second: float) -> float:
    return round(max(start_second, min(value, end_second)), 2)


def _frame_candidate(event: BrowserEvent, second: float, relative: str) -> ListenerGuidedFrame:
    return ListenerGuidedFrame(
        timestamp_second=second,
        hint=f"{summarize_browser_event(event)} | relative={relative}",
        event_id=event.event_id,
    )


def _take_spaced_candidates(
    candidates: list[ListenerGuidedFrame],
    *,
    max_frames: int,
    min_gap: float,
) -> list[ListenerGuidedFrame]:
    chosen: list[ListenerGuidedFrame] = []
    for candidate in candidates:
        if len(chosen) >= max_frames:
            break
        if any(abs(existing.timestamp_second - candidate.timestamp_second) < min_gap for existing in chosen):
            continue
        chosen.append(candidate)
    return chosen


def _merge_spaced_candidates(
    current: list[ListenerGuidedFrame],
    incoming: list[ListenerGuidedFrame],
    *,
    max_frames: int,
    min_gap: float,
) -> list[ListenerGuidedFrame]:
    chosen = list(current)
    for candidate in incoming:
        if len(chosen) >= max_frames:
            break
        if any(abs(existing.timestamp_second - candidate.timestamp_second) < min_gap for existing in chosen):
            continue
        chosen.append(candidate)
    chosen.sort(key=lambda item: item.timestamp_second)
    return chosen


def _gap_fill_candidates(
    current: list[ListenerGuidedFrame],
    *,
    max_frames: int,
    start_second: float,
    end_second: float,
) -> list[ListenerGuidedFrame]:
    if len(current) >= max_frames:
        return []

    if not current:
        midpoint = round((start_second + end_second) / 2.0, 2)
        return [ListenerGuidedFrame(timestamp_second=midpoint, hint="gap-fill | relative=midpoint", event_id="gap-fill")]

    ordered = sorted(current, key=lambda item: item.timestamp_second)
    candidate_points: list[float] = []
    if ordered[0].timestamp_second > start_second:
        candidate_points.append(round((start_second + ordered[0].timestamp_second) / 2.0, 2))
    for previous, current_item in zip(ordered, ordered[1:]):
        candidate_points.append(round((previous.timestamp_second + current_item.timestamp_second) / 2.0, 2))
    if ordered[-1].timestamp_second < end_second:
        candidate_points.append(round((ordered[-1].timestamp_second + end_second) / 2.0, 2))

    return [
        ListenerGuidedFrame(timestamp_second=point, hint="gap-fill | relative=midpoint", event_id=f"gap-{index}")
        for index, point in enumerate(candidate_points, start=1)
    ]
