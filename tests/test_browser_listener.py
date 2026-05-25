from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from src.browser_listener import (
    BrowserEventBatchIn,
    BrowserEventStore,
    _events_are_redundant,
    plan_listener_guided_frames,
    save_session_recording,
)


def test_browser_listener_store_marks_navigation_and_large_scroll_as_key_candidates() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-1",
            "events": [
                {
                    "event_type": "navigation",
                    "page_url": "https://example.com/list",
                },
                {
                    "event_type": "scroll",
                    "scroll_y": 1200,
                    "delta_y": 640,
                },
                {
                    "event_type": "focus",
                },
            ],
        }
    )

    accepted = store.ingest(batch)

    assert accepted[0].is_key_candidate is True
    assert accepted[1].is_key_candidate is True
    assert accepted[2].is_key_candidate is False


def test_browser_listener_store_returns_newest_events_first() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-2",
            "events": [
                {"event_type": "click", "target_text": "First"},
                {"event_type": "click", "target_text": "Second"},
                {"event_type": "click", "target_text": "Third"},
            ],
        }
    )
    store.ingest(batch)

    events = store.list_events(limit=2)

    assert len(events) == 2
    assert events[0].target_text == "Third"
    assert events[1].target_text == "Second"


def test_browser_listener_store_trims_long_strings() -> None:
    store = BrowserEventStore(max_events=5)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-3",
            "events": [
                {
                    "event_type": "input",
                    "input_value": "x" * 700,
                    "target_selector": ".field",
                }
            ],
        }
    )
    accepted = store.ingest(batch)

    assert accepted[0].input_value is not None
    assert len(accepted[0].input_value) <= 500


def test_browser_listener_store_persists_screenshot_and_selects_analysis_candidates() -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
    )
    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=10, artifact_root=Path(temp_dir))
        batch = BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "session-shot",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Preview",
                        "page_url": "https://example.com/report",
                        "screenshot_data_url": png_data_url,
                    }
                ],
            }
        )

        accepted = store.ingest(batch)
        session_id, selected = store.select_analysis_candidates(session_id="session-shot", limit=5)

        assert accepted[0].screenshot_path is not None
        assert Path(accepted[0].screenshot_path).exists()
        assert session_id == "session-shot"
        assert len(selected) == 1
        assert selected[0].target_text == "Preview"


def test_listener_keeps_adjacent_menu_clicks_with_different_targets() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-menu-chain",
            "events": [
                {
                    "event_type": "click",
                    "target_text": "土地供应",
                    "page_url": "https://example.com/workbench",
                    "client_timestamp_ms": 1000,
                    "key_candidate": True,
                },
                {
                    "event_type": "click",
                    "target_text": "出让公告",
                    "page_url": "https://example.com/workbench",
                    "client_timestamp_ms": 1800,
                    "key_candidate": True,
                },
            ],
        }
    )

    accepted = store.ingest(batch)

    assert _events_are_redundant(accepted[0], accepted[1]) is False


def test_plan_listener_guided_frames_uses_event_positions_and_neighbors() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-guided",
            "events": [
                {
                    "event_type": "click",
                    "target_text": "Open",
                    "client_timestamp_ms": 1000,
                    "key_candidate": True,
                },
                {
                    "event_type": "change",
                    "target_text": "Industry",
                    "client_timestamp_ms": 6000,
                    "key_candidate": True,
                },
                {
                    "event_type": "scroll",
                    "scroll_y": 900,
                    "delta_y": 600,
                    "client_timestamp_ms": 10000,
                    "key_candidate": True,
                },
            ],
        }
    )
    accepted = store.ingest(batch)

    guided = plan_listener_guided_frames(accepted, start_second=0.0, end_second=20.0, max_frames=6)

    assert 1 <= len(guided) <= 6
    assert all(0.0 <= item.timestamp_second <= 20.0 for item in guided)
    assert any("target=Open" in item.hint for item in guided)
    assert any("target=Industry" in item.hint for item in guided)
    assert guided == sorted(guided, key=lambda item: item.timestamp_second)


def test_plan_listener_guided_frames_prefers_recording_timeline_when_available() -> None:
    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=10, artifact_root=Path(temp_dir) / "listener")
        start_ms = 1_700_000_000_000
        batch = BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "timeline-session",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Open",
                        "client_timestamp_ms": start_ms + 2000,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "change",
                        "target_text": "Filter",
                        "client_timestamp_ms": start_ms + 9000,
                        "key_candidate": True,
                    },
                ],
            }
        )
        accepted = store.ingest(batch)
        recording = store.set_session_recording(
            "timeline-session",
            recording_path=str(Path(temp_dir) / "session.webm"),
            mime_type="video/webm",
            tab_id=1,
            started_at_ms=start_ms,
            ended_at_ms=start_ms + 20_000,
        )

        guided = plan_listener_guided_frames(
            accepted,
            start_second=0.0,
            end_second=20.0,
            max_frames=4,
            recording=recording,
        )

        timestamps = [item.timestamp_second for item in guided]
        assert any(abs(ts - 2.0) < 0.75 for ts in timestamps)
        assert any(abs(ts - 9.0) < 0.75 for ts in timestamps)


def test_plan_listener_guided_frames_falls_back_to_non_key_events_when_needed() -> None:
    store = BrowserEventStore(max_events=10)
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "session-fallback",
            "events": [
                {
                    "event_type": "focus",
                    "target_text": "Results panel",
                    "page_url": "https://example.com/results",
                    "client_timestamp_ms": 1000,
                    "key_candidate": False,
                }
            ],
        }
    )
    accepted = store.ingest(batch)

    guided = plan_listener_guided_frames(accepted, start_second=0.0, end_second=8.0, max_frames=3)

    assert guided
    assert all(item.timestamp_second >= 0.0 for item in guided)
    assert any("Results panel" in item.hint for item in guided)


def test_save_session_recording_and_attach_to_store(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    recording_path = save_session_recording("session-rec", "sample.webm", BytesIO(b"video-bytes"))

    store = BrowserEventStore(max_events=5, artifact_root=tmp_path / "listener")
    recording = store.set_session_recording(
        "session-rec",
        recording_path=str(recording_path),
        mime_type="video/webm",
        tab_id=12,
        started_at_ms=10,
        ended_at_ms=30,
    )
    summary = store.session_summary("session-rec")

    assert recording_path.exists()
    assert recording.session_id == "session-rec"
    assert summary["recording_path"] == str(recording_path)
    assert summary["recording_started_at_ms"] == 10


def test_browser_listener_store_restores_session_data_after_restart() -> None:
    with TemporaryDirectory() as temp_dir:
        artifact_root = Path(temp_dir) / "listener"
        recording_path = Path(temp_dir) / "recordings" / "session.webm"
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        recording_path.write_bytes(b"video-bytes")

        first_store = BrowserEventStore(max_events=10, artifact_root=artifact_root)
        first_store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "persisted-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Restore me",
                            "page_url": "https://example.com/workflow",
                            "client_timestamp_ms": 1000,
                            "key_candidate": True,
                        }
                    ],
                }
            )
        )
        first_store.set_session_recording(
            "persisted-session",
            recording_path=str(recording_path),
            mime_type="video/webm",
            tab_id=3,
            started_at_ms=100,
            ended_at_ms=900,
        )

        restarted_store = BrowserEventStore(max_events=10, artifact_root=artifact_root)
        restored_events = restarted_store.session_events("persisted-session")
        restored_recording = restarted_store.get_session_recording("persisted-session")
        restored_summary = restarted_store.session_summary("persisted-session")

        assert len(restored_events) == 1
        assert restored_events[0].target_text == "Restore me"
        assert restored_recording is not None
        assert restored_recording.recording_path == str(recording_path)
        assert restored_summary["event_count"] == 1
        assert restored_summary["key_event_count"] == 1
        assert restored_summary["session_recording_ready"] is True


def test_session_summary_reports_analysis_readiness(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = BrowserEventStore(max_events=5, artifact_root=tmp_path / "listener")
    batch = BrowserEventBatchIn.model_validate(
        {
            "client_name": "listener",
            "session_id": "summary-session",
            "events": [
                {
                    "event_type": "click",
                    "target_text": "Submit",
                    "client_timestamp_ms": 1000,
                    "key_candidate": True,
                    "screenshot_data_url": (
                        "data:image/png;base64,"
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
                    ),
                }
            ],
        }
    )
    accepted = store.ingest(batch)
    recording_path = save_session_recording("summary-session", "sample.webm", BytesIO(b"video-bytes"))
    store.set_session_recording(
        "summary-session",
        recording_path=str(recording_path),
        mime_type="video/webm",
        tab_id=5,
        started_at_ms=10,
        ended_at_ms=20,
    )

    summary = store.session_summary("summary-session")
    latest_summary = store.latest_session_summary()
    status = store.status()

    assert accepted[0].is_key_candidate is True
    assert summary["has_recording"] is True
    assert summary["has_screenshots"] is True
    assert summary["listener_analysis_ready"] is True
    assert summary["session_recording_ready"] is True
    assert latest_summary is not None
    assert latest_summary["session_id"] == "summary-session"
    assert status["latest_session_summary"]["session_id"] == "summary-session"


def test_session_summary_separates_listener_analysis_from_session_recording() -> None:
    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=5, artifact_root=Path(temp_dir) / "listener")
        batch = BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "no-recording-session",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Continue",
                        "client_timestamp_ms": 1000,
                        "key_candidate": True,
                        "screenshot_data_url": (
                            "data:image/png;base64,"
                            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
                        ),
                    }
                ],
            }
        )
        store.ingest(batch)

        summary = store.session_summary("no-recording-session")

        assert summary["listener_analysis_ready"] is True
        assert summary["session_recording_ready"] is False
