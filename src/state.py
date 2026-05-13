from __future__ import annotations

from pathlib import Path

import streamlit as st


STATE_DEFAULTS = {
    "selected_video_path": None,
    "planned_timestamps": [],
    "extracted_frame_paths": [],
    "analysis_result": None,
    "replay_session": None,
    "replay_logs": [],
    "live_browser_cdp_url": "http://127.0.0.1:9222",
}


def initialize_state() -> None:
    for key, value in STATE_DEFAULTS.items():
        st.session_state.setdefault(key, value)


def set_selected_video(video_path: Path) -> None:
    st.session_state["selected_video_path"] = str(video_path)


def set_planned_timestamps(timestamps: list[float]) -> None:
    st.session_state["planned_timestamps"] = timestamps


def set_extracted_frames(frame_paths: list[Path]) -> None:
    st.session_state["extracted_frame_paths"] = [str(path) for path in frame_paths]


def get_extracted_frames() -> list[Path]:
    return [Path(path) for path in st.session_state.get("extracted_frame_paths", [])]


def set_analysis_result(result: object) -> None:
    st.session_state["analysis_result"] = result


def get_analysis_result() -> object:
    return st.session_state.get("analysis_result")


def set_replay_session(session: object) -> None:
    st.session_state["replay_session"] = session


def get_replay_session() -> object:
    return st.session_state.get("replay_session")


def set_replay_logs(logs: list[str]) -> None:
    st.session_state["replay_logs"] = logs


def get_replay_logs() -> list[str]:
    return st.session_state.get("replay_logs", [])


def set_live_browser_cdp_url(cdp_url: str) -> None:
    st.session_state["live_browser_cdp_url"] = cdp_url


def get_live_browser_cdp_url() -> str:
    return st.session_state.get("live_browser_cdp_url", "http://127.0.0.1:9222")
