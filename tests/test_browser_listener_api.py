import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import src.webapp as webapp_module
from src.browser_listener import BrowserEventBatchIn, BrowserEventStore, choose_site_url
from src.dsl import ReplayBundle


class DummyRequest:
    def __init__(self, payload: dict):
        self._payload = payload
        self.query_params = {}

    async def json(self) -> dict:
        return self._payload


def test_choose_site_url_skips_console_and_search_pages() -> None:
    store = BrowserEventStore(max_events=10)
    events = store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "site-url-session",
                "events": [
                    {"event_type": "page_loaded", "page_url": "http://127.0.0.1:8018/app"},
                    {"event_type": "click", "page_url": "https://www.bing.com/search?q=target"},
                    {"event_type": "tab_updated", "page_url": "https://example.com/workflow"},
                ],
            }
        )
    )

    assert choose_site_url(events, "https://fallback.example") == "https://example.com/workflow"


def test_ingest_marks_eyeclaw_console_events_as_non_key_without_screenshots(tmp_path) -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
    )
    store = BrowserEventStore(max_events=10, artifact_root=tmp_path)

    events = store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "console-filter-session",
                "events": [
                    {
                        "event_type": "click",
                        "page_url": "http://127.0.0.1:8018/app",
                        "page_title": "Eyeclaw 用户端",
                        "target_text": "保存技能",
                        "screenshot_data_url": png_data_url,
                    }
                ],
            }
        )
    )
    session_id, candidates = store.select_analysis_candidates("console-filter-session")

    assert session_id == "console-filter-session"
    assert events[0].is_key_candidate is False
    assert events[0].screenshot_path is None
    assert candidates == []


def test_browser_listener_analyze_route_uses_candidate_screenshots() -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
    )
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_load_config_status = webapp_module.load_config_status
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir))
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "api-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Open report",
                            "page_url": "https://example.com/report",
                            "screenshot_data_url": png_data_url,
                        }
                    ],
                }
            )
        )

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_build_replay_plan(
            frame_paths,
            config,
            user_request=None,
            site_url=None,
            frame_hints=None,
            progress_callback=None,
        ):
            assert len(frame_paths) == 1
            assert frame_hints is not None
            assert "click" in frame_hints[0]
            return SimpleNamespace(
                sop=["Open the report from the current page."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Open the report from the current page."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Open report",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            response = asyncio.run(
                webapp_module.analyze_browser_listener_session(
                    DummyRequest({"session_id": "api-session", "max_events": 4})
                )
            )
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.load_config_status = original_load_config_status
            webapp_module.build_replay_plan = original_build_replay_plan

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "api-session" in payload
    assert "Open report" in payload


def test_video_analyze_route_requires_listener_guidance_and_uses_listener_session() -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_load_config_status = webapp_module.load_config_status
    original_get_video_metadata = webapp_module.get_video_metadata
    original_extract_frames = webapp_module.extract_frames
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir) / "listener")
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "video-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Search",
                            "client_timestamp_ms": 1000,
                            "key_candidate": True,
                        },
                        {
                            "event_type": "change",
                            "target_text": "Province",
                            "client_timestamp_ms": 5000,
                            "key_candidate": True,
                        },
                    ],
                }
            )
        )

        fake_video = Path(temp_dir) / "fake.mp4"
        fake_video.write_bytes(b"not-a-real-video")
        generated_frames = []

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_get_video_metadata(path: Path):
            return SimpleNamespace(duration_seconds=20.0, fps=10.0, width=100, height=100)

        def fake_extract_frames(video_path, timestamps, job_id):
            paths = []
            for index, _ in enumerate(timestamps, start=1):
                frame_path = Path(temp_dir) / f"{job_id}_{index:02d}.png"
                frame_path.write_bytes(b"frame")
                paths.append(frame_path)
            generated_frames[:] = paths
            return paths

        def fake_build_replay_plan(
            frame_paths,
            config,
            user_request=None,
            site_url=None,
            frame_hints=None,
            progress_callback=None,
        ):
            assert len(frame_paths) == len(generated_frames)
            assert frame_hints is not None
            assert any("target=Search" in hint for hint in frame_hints)
            return SimpleNamespace(
                sop=["Use listener-guided frames."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Use listener-guided frames."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Search",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.get_video_metadata = fake_get_video_metadata
        webapp_module.extract_frames = fake_extract_frames
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            response = asyncio.run(
                webapp_module.analyze_video(
                    DummyRequest(
                        {
                            "video_path": str(fake_video),
                            "listener_session_id": "video-session",
                            "start_second": 0.0,
                            "end_second": 20.0,
                            "max_frames": 6,
                        }
                    )
                )
            )
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.load_config_status = original_load_config_status
            webapp_module.get_video_metadata = original_get_video_metadata
            webapp_module.extract_frames = original_extract_frames
            webapp_module.build_replay_plan = original_build_replay_plan

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "\"listener_guided\":true" in payload
    assert "video-session" in payload


def test_video_analyze_route_uses_session_recording_when_video_path_missing() -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_load_config_status = webapp_module.load_config_status
    original_get_video_metadata = webapp_module.get_video_metadata
    original_extract_frames = webapp_module.extract_frames
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir) / "listener")
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "recording-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Search",
                            "client_timestamp_ms": 1000,
                            "key_candidate": True,
                        }
                    ],
                }
            )
        )
        recording_path = Path(temp_dir) / "recording.webm"
        recording_path.write_bytes(b"webm")
        store.set_session_recording(
            "recording-session",
            recording_path=str(recording_path),
            mime_type="video/webm",
            tab_id=1,
            started_at_ms=10,
            ended_at_ms=20,
        )

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_get_video_metadata(path: Path):
            assert path == recording_path
            return SimpleNamespace(duration_seconds=20.0, fps=10.0, width=100, height=100)

        def fake_extract_frames(video_path, timestamps, job_id):
            assert video_path == recording_path
            frame_path = Path(temp_dir) / f"{job_id}_01.png"
            frame_path.write_bytes(b"frame")
            return [frame_path]

        def fake_build_replay_plan(
            frame_paths,
            config,
            user_request=None,
            site_url=None,
            frame_hints=None,
            progress_callback=None,
        ):
            return SimpleNamespace(
                sop=["Use session recording."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Use session recording."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Search",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.get_video_metadata = fake_get_video_metadata
        webapp_module.extract_frames = fake_extract_frames
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            response = asyncio.run(
                webapp_module.analyze_video(
                    DummyRequest(
                        {
                            "listener_session_id": "recording-session",
                            "start_second": 0.0,
                            "end_second": 20.0,
                            "max_frames": 4,
                        }
                    )
                )
            )
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.load_config_status = original_load_config_status
            webapp_module.get_video_metadata = original_get_video_metadata
            webapp_module.extract_frames = original_extract_frames
            webapp_module.build_replay_plan = original_build_replay_plan

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "recording-session" in payload
    assert "\"listener_guided\":true" in payload


def test_latest_session_summary_route_returns_recording_and_event_counts() -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir) / "listener")
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "latest-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Save",
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
        )
        recording_path = Path(temp_dir) / "latest.webm"
        recording_path.write_bytes(b"webm")
        store.set_session_recording(
            "latest-session",
            recording_path=str(recording_path),
            mime_type="video/webm",
            tab_id=1,
            started_at_ms=1,
            ended_at_ms=2,
        )

        webapp_module.BROWSER_EVENT_STORE = store
        try:
            response = asyncio.run(webapp_module.browser_listener_latest_session_summary(DummyRequest({})))
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store

    assert response.status_code == 200
    payload = response.body.decode("utf-8")
    assert "latest-session" in payload
    assert "\"has_recording\":true" in payload
    assert "\"listener_analysis_ready\":true" in payload
    assert "\"session_recording_ready\":true" in payload


def test_recordings_route_returns_playable_video_and_smart_title(tmp_path, monkeypatch) -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE

    monkeypatch.chdir(tmp_path)
    store = BrowserEventStore(max_events=20, artifact_root=tmp_path / "listener")
    store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "recording-list-session",
                "events": [
                    {
                        "event_type": "tab_updated",
                        "page_title": "中国土地市场网",
                        "page_url": "https://example.com/land",
                        "client_timestamp_ms": 1000,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "click",
                        "target_text": "搜索",
                        "page_title": "中国土地市场网",
                        "page_url": "https://example.com/land",
                        "client_timestamp_ms": 2000,
                        "key_candidate": True,
                    },
                ],
            }
        )
    )
    recording_path = tmp_path / "recording.webm"
    recording_path.write_bytes(b"webm")
    store.set_session_recording(
        "recording-list-session",
        recording_path=str(recording_path),
        mime_type="video/webm",
        tab_id=1,
        started_at_ms=1,
        ended_at_ms=2,
    )

    webapp_module.BROWSER_EVENT_STORE = store
    try:
        request = DummyRequest({})
        request.query_params = {"limit": "10"}
        response = asyncio.run(webapp_module.list_browser_listener_recordings(request))
    finally:
        webapp_module.BROWSER_EVENT_STORE = original_store

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["count"] == 1
    recording = payload["recordings"][0]
    assert recording["session_id"] == "recording-list-session"
    assert recording["video_url"].endswith("session_id=recording-list-session")
    assert "中国土地市场网" in recording["title"]
    assert "搜索" in recording["title"]


def test_recordings_route_includes_listener_only_sessions(tmp_path, monkeypatch) -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE

    monkeypatch.chdir(tmp_path)
    store = BrowserEventStore(max_events=20, artifact_root=tmp_path / "listener")
    store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "listener-only-session",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Search",
                        "page_title": "Example Site",
                        "page_url": "https://example.com/",
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
    )

    webapp_module.BROWSER_EVENT_STORE = store
    try:
        request = DummyRequest({})
        request.query_params = {"limit": "10"}
        response = asyncio.run(webapp_module.list_browser_listener_recordings(request))
    finally:
        webapp_module.BROWSER_EVENT_STORE = original_store

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["count"] == 1
    recording = payload["recordings"][0]
    assert recording["session_id"] == "listener-only-session"
    assert recording["has_recording"] is False
    assert recording["video_url"] is None
    assert recording["listener_analysis_ready"] is True
    assert recording["event_count"] == 1


def test_recording_title_prefers_target_site_over_search_engine() -> None:
    store = BrowserEventStore(max_events=20)
    events = store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "search-title-session",
                "events": [
                    {
                        "event_type": "tab_updated",
                        "page_title": "中国土地市场网 - 必应搜索",
                        "page_url": "https://cn.bing.com/search?q=%E4%B8%AD%E5%9B%BD%E5%9C%9F%E5%9C%B0%E5%B8%82%E5%9C%BA%E7%BD%91",
                        "client_timestamp_ms": 1000,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "input",
                        "target_text": "搜索框",
                        "input_value": "中国土地市场网",
                        "page_url": "https://cn.bing.com/search",
                        "client_timestamp_ms": 1200,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "click",
                        "target_text": "中国土地市场网",
                        "page_url": "https://cn.bing.com/search",
                        "client_timestamp_ms": 1800,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "tab_updated",
                        "page_title": "中国土地市场网",
                        "page_url": "https://www.landchina.com/",
                        "client_timestamp_ms": 2600,
                        "key_candidate": True,
                    },
                ],
            }
        )
    )

    title = webapp_module._build_recording_title(events)

    assert "中国土地市场网" in title
    assert "必应" not in title
    assert "搜索" in title


def test_placeholder_skill_name_uses_listener_semantics() -> None:
    store = BrowserEventStore(max_events=20)
    events = store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "skill-title-session",
                "events": [
                    {
                        "event_type": "tab_updated",
                        "page_title": "中国土地市场网",
                        "page_url": "https://www.landchina.com/",
                        "client_timestamp_ms": 1000,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "change",
                        "target_text": "浙江省",
                        "page_url": "https://www.landchina.com/",
                        "client_timestamp_ms": 2000,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "change",
                        "target_text": "杭州市",
                        "page_url": "https://www.landchina.com/",
                        "client_timestamp_ms": 2400,
                        "key_candidate": True,
                    },
                    {
                        "event_type": "click",
                        "target_text": "查询",
                        "page_url": "https://www.landchina.com/",
                        "client_timestamp_ms": 3000,
                        "key_candidate": True,
                    },
                ],
            }
        )
    )

    title = webapp_module._build_skill_title(
        steps=[{"step_number": 1, "action": "click", "target": "查询"}],
        site_url="https://www.landchina.com/",
        user_request="请帮我完成这一段演示操作，并保存成技能",
        listener_events=events,
    )

    assert title.startswith("中国土地市场网")
    assert "筛选" in title
    assert "请帮我" not in title


def test_recordings_route_falls_back_to_disk_files_after_restart(tmp_path, monkeypatch) -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    recordings_root = tmp_path / "artifacts" / "session_recordings" / "disk-session"
    recordings_root.mkdir(parents=True)
    recording_path = recordings_root / "session_recording.webm"
    recording_path.write_bytes(b"webm")

    monkeypatch.chdir(tmp_path)
    webapp_module.BROWSER_EVENT_STORE = BrowserEventStore(max_events=20, artifact_root=tmp_path / "listener")
    try:
        request = DummyRequest({})
        request.query_params = {"limit": "10"}
        response = asyncio.run(webapp_module.list_browser_listener_recordings(request))
    finally:
        webapp_module.BROWSER_EVENT_STORE = original_store

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["count"] == 1
    recording = payload["recordings"][0]
    assert recording["session_id"] == "disk-session"
    assert recording["title"] == "Recording disk-ses"
    assert recording["video_url"].endswith("session_id=disk-session")


def test_recordings_route_restores_persisted_listener_counts_after_restart(tmp_path, monkeypatch) -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    monkeypatch.chdir(tmp_path)

    live_store = BrowserEventStore(max_events=20, artifact_root=tmp_path / "listener")
    live_store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "persisted-session",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Search",
                        "page_title": "中国土地市场网",
                        "page_url": "https://www.landchina.com/",
                        "client_timestamp_ms": 1000,
                        "key_candidate": True,
                    }
                ],
            }
        )
    )
    recordings_root = tmp_path / "artifacts" / "session_recordings" / "persisted-session"
    recordings_root.mkdir(parents=True)
    recording_path = recordings_root / "session_recording.webm"
    recording_path.write_bytes(b"webm")
    live_store.set_session_recording(
        "persisted-session",
        recording_path=str(recording_path),
        mime_type="video/webm",
        tab_id=1,
        started_at_ms=1,
        ended_at_ms=2,
    )

    webapp_module.BROWSER_EVENT_STORE = BrowserEventStore(max_events=20, artifact_root=tmp_path / "listener")
    try:
        request = DummyRequest({})
        request.query_params = {"limit": "10"}
        response = asyncio.run(webapp_module.list_browser_listener_recordings(request))
    finally:
        webapp_module.BROWSER_EVENT_STORE = original_store

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["count"] == 1
    recording = payload["recordings"][0]
    assert recording["session_id"] == "persisted-session"
    assert recording["event_count"] == 1
    assert recording["key_event_count"] == 1
    assert "Recording " not in recording["title"]


def test_delete_recording_route_removes_video_but_keeps_events(tmp_path, monkeypatch) -> None:
    original_store = webapp_module.BROWSER_EVENT_STORE
    monkeypatch.chdir(tmp_path)

    store = BrowserEventStore(max_events=20, artifact_root=tmp_path / "listener")
    store.ingest(
        BrowserEventBatchIn.model_validate(
            {
                "client_name": "listener",
                "session_id": "delete-session",
                "events": [
                    {
                        "event_type": "click",
                        "target_text": "Delete me",
                        "client_timestamp_ms": 1000,
                        "key_candidate": True,
                    }
                ],
            }
        )
    )
    recording_dir = tmp_path / "artifacts" / "session_recordings" / "delete-session"
    recording_dir.mkdir(parents=True)
    recording_path = recording_dir / "session_recording.webm"
    recording_path.write_bytes(b"webm")
    store.set_session_recording(
        "delete-session",
        recording_path=str(recording_path),
        mime_type="video/webm",
        tab_id=1,
        started_at_ms=1,
        ended_at_ms=2,
    )

    webapp_module.BROWSER_EVENT_STORE = store
    try:
        request = DummyRequest({})
        request.query_params = {"session_id": "delete-session"}
        response = asyncio.run(webapp_module.delete_browser_listener_recording(request))
        summary = store.session_summary("delete-session")
    finally:
        webapp_module.BROWSER_EVENT_STORE = original_store

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["deleted"] is True
    assert not recording_path.exists()
    assert summary["event_count"] == 1
    assert summary["session_recording_ready"] is False


def test_listener_analysis_job_routes_report_progress_and_result() -> None:
    png_data_url = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z7nQAAAAASUVORK5CYII="
    )
    original_store = webapp_module.BROWSER_EVENT_STORE
    original_jobs = webapp_module.ANALYSIS_JOBS
    original_load_config_status = webapp_module.load_config_status
    original_build_replay_plan = webapp_module.build_replay_plan

    with TemporaryDirectory() as temp_dir:
        store = BrowserEventStore(max_events=20, artifact_root=Path(temp_dir))
        store.ingest(
            BrowserEventBatchIn.model_validate(
                {
                    "client_name": "listener",
                    "session_id": "job-session",
                    "events": [
                        {
                            "event_type": "click",
                            "target_text": "Run analysis",
                            "page_url": "https://example.com/report",
                            "screenshot_data_url": png_data_url,
                        }
                    ],
                }
            )
        )

        def fake_load_config_status():
            return SimpleNamespace(
                is_ready=True,
                config=SimpleNamespace(
                    target_site_url="https://example.com",
                    glm_model="stub-glm",
                    deepseek_model="stub-deepseek",
                ),
            )

        def fake_build_replay_plan(
            frame_paths,
            config,
            user_request=None,
            site_url=None,
            frame_hints=None,
            progress_callback=None,
        ):
            return SimpleNamespace(
                sop=["Analyze the latest listener session."],
                replay_bundle=ReplayBundle.model_validate(
                    {
                        "sop": ["Analyze the latest listener session."],
                        "plan": {
                            "site_url": site_url or "https://example.com",
                            "steps": [
                                {
                                    "step_number": 1,
                                    "action": "click",
                                    "target": "Run analysis",
                                }
                            ],
                        },
                        "assumptions": [],
                    }
                ),
                raw_glm_output={"uncertainties": []},
            )

        async def exercise_job_flow() -> tuple[dict, dict]:
            start_response = await webapp_module.start_listener_analysis_job(
                DummyRequest({"session_id": "job-session", "max_events": 4})
            )
            start_payload = json.loads(start_response.body)
            status_request = DummyRequest({})
            status_request.query_params = {"job_id": start_payload["job_id"]}

            for _ in range(20):
                status_response = await webapp_module.analysis_job_status(status_request)
                status_payload = json.loads(status_response.body)
                if status_payload["status"] == "completed":
                    return start_payload, status_payload
                await asyncio.sleep(0.01)

            raise AssertionError("analysis job did not complete in time")

        webapp_module.BROWSER_EVENT_STORE = store
        webapp_module.ANALYSIS_JOBS = webapp_module.AnalysisJobStore()
        webapp_module.load_config_status = fake_load_config_status
        webapp_module.build_replay_plan = fake_build_replay_plan

        try:
            start_payload, status_payload = asyncio.run(exercise_job_flow())
        finally:
            webapp_module.BROWSER_EVENT_STORE = original_store
            webapp_module.ANALYSIS_JOBS = original_jobs
            webapp_module.load_config_status = original_load_config_status
            webapp_module.build_replay_plan = original_build_replay_plan

    assert start_payload["status"] == "running"
    assert status_payload["status"] == "completed"
    assert status_payload["progress_percent"] == 100
    assert status_payload["result"]["session_id"] == "job-session"


def test_analysis_job_store_reports_running_progress_as_activity() -> None:
    store = webapp_module.AnalysisJobStore()
    job = store.create_job("video_analysis", stage="正在抽取关键帧...")
    store.update(job.id, progress_percent=82, stage="正在调用多模态模型分析...")

    stored_job = store.get(job.id)
    assert stored_job is not None
    stored_job.stage_started_at_monotonic -= 30.0

    payload = store.as_dict(job.id)

    assert payload is not None
    assert payload["reported_progress_percent"] == 82
    assert payload["progress_percent"] == 82
    assert payload["progress_mode"] == "activity"
    assert payload["progress_estimated"] is True


def test_model_input_summary_reports_real_frame_and_batch_counts() -> None:
    assert webapp_module._model_input_summary(0, "关键帧") == "实际送入模型：0 张关键帧。"
    assert webapp_module._model_input_summary(1, "关键帧") == "实际送入模型：1 张关键帧，分 1 批。"
    assert webapp_module._model_input_summary(12, "候选截图") == "实际送入模型：12 张候选截图，分 4 批。"
