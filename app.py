from pathlib import Path
from uuid import uuid4

import streamlit as st

from src.analyze import build_replay_plan
from src.config import load_config_status
from src.eia_workflow import DEFAULT_CDP_URL, ManualCheckpointRequired, detect_eia_state, run_eia_live_workflow
from src.replay import close_replay_session, connect_over_cdp, run_replay_plan, start_replay_session
from src.state import (
    get_analysis_result,
    get_extracted_frames,
    get_live_browser_cdp_url,
    get_replay_logs,
    get_replay_session,
    initialize_state,
    set_analysis_result,
    set_extracted_frames,
    set_live_browser_cdp_url,
    set_planned_timestamps,
    set_replay_logs,
    set_replay_session,
    set_selected_video,
)
from src.video import (
    extract_frames,
    format_duration,
    get_video_metadata,
    plan_frame_timestamps,
    save_uploaded_video,
)


st.set_page_config(
    page_title="Show Once",
    layout="wide",
)


def render_config_status() -> None:
    status = load_config_status()
    if status.is_ready:
        config = status.config
        assert config is not None
        st.success("Configuration loaded. Ready to analyze video and prepare Edge replay.")
        st.caption(
            f"Target site: `{config.target_site_url}`  |  "
            f"DeepSeek: `{config.deepseek_model}`  |  "
            f"GLM: `{config.glm_model}`  |  "
            f"Edge profile: `{config.edge_user_data_dir}`  |  "
            f"Edge channel: `{config.edge_channel}`"
        )
        return

    st.warning("Configuration is incomplete. Create `.env` before running analysis or replay.")
    if status.missing_fields:
        st.caption("Missing fields: " + ", ".join(status.missing_fields))
    st.code("python scripts\\import_model_txt.py", language="powershell")


def render_video_source() -> None:
    default_video = Path("website.mp4")
    st.subheader("1. Video Source")
    selected_video = default_video

    if default_video.exists():
        st.info(f"Detected local demo video: `{default_video}`")
        st.video(str(default_video))
    else:
        st.error("Default demo video `website.mp4` was not found in the workspace.")

    uploaded_video = st.file_uploader(
        "Optional: upload a different MP4 for analysis",
        type=["mp4"],
        accept_multiple_files=False,
    )
    if uploaded_video is not None:
        st.caption(f"Uploaded override: `{uploaded_video.name}`")
        selected_video = save_uploaded_video(uploaded_video.name, uploaded_video)

    if selected_video.exists():
        set_selected_video(selected_video)
        render_video_analysis_controls(selected_video)


def render_video_analysis_controls(video_path: Path) -> None:
    st.subheader("2. Segment Planning")

    metadata = get_video_metadata(video_path)
    st.caption(
        f"Duration: `{format_duration(metadata.duration_seconds)}`  |  "
        f"FPS: `{metadata.fps:.2f}`  |  "
        f"Resolution: `{metadata.width}x{metadata.height}`"
    )

    default_end = min(metadata.duration_seconds, 20.0) if metadata.duration_seconds else 20.0
    start_second, end_second = st.slider(
        "Pick the most meaningful part of the demo video",
        min_value=0.0,
        max_value=max(metadata.duration_seconds, 1.0),
        value=(0.0, default_end),
        step=0.5,
    )
    max_frames = st.slider("How many key frames to sample", min_value=3, max_value=12, value=8, step=1)

    timestamps = plan_frame_timestamps(
        duration_seconds=metadata.duration_seconds,
        start_second=start_second,
        end_second=end_second,
        max_frames=max_frames,
    )
    set_planned_timestamps(timestamps)

    st.write("Planned timestamps")
    st.code(", ".join(f"{timestamp:.2f}s" for timestamp in timestamps), language="text")

    if st.button("Extract Preview Frames", use_container_width=True):
        frame_paths = extract_frames(video_path, timestamps, job_id=uuid4().hex[:8])
        if not frame_paths:
            st.error("No frames were extracted from the selected segment.")
        else:
            set_extracted_frames(frame_paths)
            set_analysis_result(None)
            st.success(f"Extracted {len(frame_paths)} frames.")

    frame_paths = get_extracted_frames()
    if frame_paths:
        columns = st.columns(min(4, len(frame_paths)))
        for index, frame_path in enumerate(frame_paths):
            columns[index % len(columns)].image(str(frame_path), caption=frame_path.name)


def render_analysis_section() -> None:
    st.subheader("3. Analysis")
    status = load_config_status()
    frame_paths = get_extracted_frames()
    analysis_result = get_analysis_result()

    if not frame_paths:
        st.info("Extract preview frames first.")
        return

    if not status.is_ready or status.config is None:
        st.warning("Configuration is required before analysis can run.")
        return

    if st.button("Analyze Extracted Frames", use_container_width=True):
        with st.spinner("Generating SOP and replay plan from extracted frames..."):
            try:
                analysis_result = build_replay_plan(frame_paths=frame_paths, config=status.config)
            except Exception as exc:
                st.error(f"Analysis failed: {exc}")
                set_analysis_result(None)
            else:
                set_analysis_result(analysis_result)

    analysis_result = get_analysis_result()
    if analysis_result is None:
        st.caption("No analysis result yet.")
        return

    st.write("SOP")
    for step in analysis_result.sop:
        st.markdown(f"- {step}")

    st.write("Replay DSL")
    st.code(analysis_result.replay_bundle.plan.to_json(), language="json")

    uncertainties = analysis_result.raw_glm_output.get("uncertainties", [])
    if uncertainties:
        st.warning("\n".join(uncertainties))


def render_replay_section() -> None:
    st.subheader("4. Edge Replay")
    status = load_config_status()
    analysis_result = get_analysis_result()
    replay_session = get_replay_session()
    current_cdp_url = get_live_browser_cdp_url()

    if not status.is_ready or status.config is None:
        st.info("Create `.env` before launching Edge replay.")
        return

    replay_mode = st.radio(
        "Replay mode",
        options=["AutoGLM-style live browser", "Generic DSL replay"],
        index=0,
        horizontal=True,
    )

    if replay_mode == "AutoGLM-style live browser":
        render_live_browser_section(status.config, replay_session, current_cdp_url)
        return

    if analysis_result is None:
        st.info("Generate a replay plan first.")
        return

    if replay_session is None:
        if st.button("Launch Edge Session For Login", use_container_width=True):
            try:
                replay_session = start_replay_session(status.config)
            except Exception as exc:
                st.error(f"Unable to start Edge session: {exc}")
            else:
                set_replay_session(replay_session)
                set_replay_logs(
                    [
                        "Edge session launched.",
                        "Scan-login manually in the opened browser window, then come back and run replay.",
                    ]
                )
                st.success("Edge launched. Complete manual login, then run replay.")
        return

    st.success("Edge session is open. Finish manual login in that window before replay.")

    if st.button("Run Replay Plan", use_container_width=True):
        progress_placeholder = st.empty()

        def report(message: str) -> None:
            existing_logs = get_replay_logs()
            existing_logs.append(message)
            set_replay_logs(existing_logs)
            progress_placeholder.code("\n".join(existing_logs), language="text")

        try:
            set_replay_logs(["Replay started."])
            logs = run_replay_plan(
                session=replay_session,
                replay_plan=analysis_result.replay_bundle.plan,
                progress_callback=report,
            )
        except Exception as exc:
            existing_logs = get_replay_logs()
            existing_logs.append(f"Replay failed: {exc}")
            set_replay_logs(existing_logs)
            st.error(f"Replay failed: {exc}")
        else:
            set_replay_logs(logs)
            st.success("Replay completed.")

    if st.button("Close Edge Session", use_container_width=True):
        close_replay_session(replay_session)
        set_replay_session(None)
        set_replay_logs([])
        st.info("Edge session closed.")

    replay_logs = get_replay_logs()
    if replay_logs:
        st.code("\n".join(replay_logs), language="text")


def render_live_browser_section(config, replay_session, current_cdp_url: str) -> None:
    st.caption(
        "Recommended for demos: connect to an already open Edge window, pause for QR login when needed, "
        "then run the site-specific 大众环评 flow."
    )
    cdp_url = st.text_input("CDP URL", value=current_cdp_url)
    set_live_browser_cdp_url(cdp_url)

    left, right = st.columns(2)
    with left:
        if st.button("Connect To Live Edge", use_container_width=True):
            try:
                replay_session = connect_over_cdp(cdp_url)
            except Exception as exc:
                st.error(f"Unable to connect to the live browser: {exc}")
            else:
                set_replay_session(replay_session)
                set_replay_logs([f"Connected to live browser via {cdp_url}."])
                st.success("Connected to the live Edge session.")

    with right:
        if replay_session is not None and st.button("Close Live Browser Session", use_container_width=True):
            close_replay_session(replay_session)
            set_replay_session(None)
            set_replay_logs([])
            st.info("Detached from the live browser session.")

    replay_session = get_replay_session()
    if replay_session is None:
        st.info("Open Edge with remote debugging, then connect here.")
        return

    try:
        state = detect_eia_state(replay_session)
    except Exception as exc:
        st.warning(f"Connected, but unable to inspect the current page yet: {exc}")
    else:
        st.success(f"Current page state: `{state.page_role}` on `{state.title}`")
        st.caption(state.summary)

    if st.button("Run 大众环评 Live Workflow", use_container_width=True):
        progress_placeholder = st.empty()
        set_replay_logs(["Live workflow started."])

        def report(message: str) -> None:
            existing_logs = get_replay_logs()
            existing_logs.append(message)
            set_replay_logs(existing_logs)
            progress_placeholder.code("\n".join(existing_logs), language="text")

        try:
            logs = run_eia_live_workflow(replay_session, progress_callback=report)
        except ManualCheckpointRequired as exc:
            existing_logs = get_replay_logs()
            existing_logs.append(f"Manual checkpoint: {exc}")
            set_replay_logs(existing_logs)
            st.warning(str(exc))
        except Exception as exc:
            existing_logs = get_replay_logs()
            existing_logs.append(f"Live workflow failed: {exc}")
            set_replay_logs(existing_logs)
            st.error(f"Live workflow failed: {exc}")
        else:
            set_replay_logs(logs)
            st.success("Live workflow completed.")

    replay_logs = get_replay_logs()
    if replay_logs:
        st.code("\n".join(replay_logs), language="text")


def main() -> None:
    initialize_state()
    st.title("Show Once")
    st.write("You do not prompt the browser. You teach it.")

    render_config_status()
    render_video_source()

    left, right = st.columns(2)
    with left:
        render_analysis_section()
    with right:
        render_replay_section()


if __name__ == "__main__":
    main()
