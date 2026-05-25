from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import BinaryIO, Iterable, List

import cv2


ARTIFACTS_DIR = Path("artifacts")
UPLOADS_DIR = ARTIFACTS_DIR / "uploads"
FRAMES_DIR = ARTIFACTS_DIR / "frames"


@dataclass(frozen=True)
class VideoMetadata:
    path: Path
    duration_seconds: float
    fps: float
    frame_count: int
    width: int
    height: int


def save_uploaded_video(upload_name: str, stream: BinaryIO) -> Path:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    target_path = UPLOADS_DIR / upload_name
    target_path.write_bytes(stream.read())
    return target_path


def get_video_metadata(video_path: Path) -> VideoMetadata:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    duration_seconds = frame_count / fps if fps > 0 and frame_count > 0 else 0.0

    if not _metadata_is_sane(duration_seconds=duration_seconds, frame_count=frame_count, fps=fps):
        scanned_count, scanned_duration = _scan_video_duration(capture, fps=fps)
        if scanned_count > 0:
            frame_count = scanned_count
        if scanned_duration > 0:
            duration_seconds = scanned_duration

    capture.release()

    return VideoMetadata(
        path=video_path,
        duration_seconds=duration_seconds,
        fps=fps,
        frame_count=frame_count,
        width=width,
        height=height,
    )


def clamp_segment(duration_seconds: float, start_second: float, end_second: float) -> tuple[float, float]:
    if duration_seconds <= 0:
        return 0.0, 0.0

    bounded_start = max(0.0, min(start_second, duration_seconds))
    bounded_end = max(bounded_start, min(end_second, duration_seconds))
    return bounded_start, bounded_end


def plan_frame_timestamps(
    duration_seconds: float,
    start_second: float,
    end_second: float,
    max_frames: int,
) -> List[float]:
    if max_frames <= 0:
        return []

    start_second, end_second = clamp_segment(duration_seconds, start_second, end_second)
    if duration_seconds <= 0 or end_second <= start_second:
        return [round(start_second, 2)]

    if max_frames == 1:
        return [round(start_second, 2)]

    segment_duration = end_second - start_second
    step = segment_duration / (max_frames - 1)
    timestamps = [round(start_second + index * step, 2) for index in range(max_frames)]
    timestamps[-1] = round(end_second, 2)
    return timestamps


def extract_frames(video_path: Path, timestamps: Iterable[float], job_id: str) -> List[Path]:
    target_dir = FRAMES_DIR / job_id
    target_dir.mkdir(parents=True, exist_ok=True)
    requested_timestamps = [max(float(timestamp), 0.0) for timestamp in timestamps]
    if not requested_timestamps:
        return []

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    frame_paths: List[Path] = []
    for index, timestamp in enumerate(requested_timestamps, start=1):
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
        success, frame = capture.read()
        if not success:
            continue

        frame_path = target_dir / f"frame_{index:02d}.png"
        cv2.imwrite(str(frame_path), frame)
        frame_paths.append(frame_path)

    capture.release()
    if len(frame_paths) < len(requested_timestamps):
        sequential_paths = _extract_frames_sequential(video_path, requested_timestamps, target_dir)
        if len(sequential_paths) > len(frame_paths):
            return sequential_paths

    return frame_paths


def format_duration(duration_seconds: float) -> str:
    total_seconds = max(0, int(round(duration_seconds)))
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def _metadata_is_sane(*, duration_seconds: float, frame_count: int, fps: float) -> bool:
    if frame_count <= 0 or fps <= 0:
        return False
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return False
    return duration_seconds < 24 * 60 * 60


def _scan_video_duration(capture: cv2.VideoCapture, *, fps: float) -> tuple[int, float]:
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    frame_count = 0
    last_position_ms = 0.0

    while True:
        success, _frame = capture.read()
        if not success:
            break
        frame_count += 1
        position_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
        if math.isfinite(position_ms) and position_ms >= 0:
            last_position_ms = max(last_position_ms, position_ms)

    duration_from_timestamp = last_position_ms / 1000.0 if last_position_ms > 0 else 0.0
    duration_from_fps = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
    duration_seconds = duration_from_timestamp or duration_from_fps
    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
    return frame_count, duration_seconds


def _extract_frames_sequential(video_path: Path, timestamps: list[float], target_dir: Path) -> List[Path]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise ValueError(f"Unable to open video: {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    indexed_targets = sorted(
        [(index, max(float(timestamp), 0.0)) for index, timestamp in enumerate(timestamps, start=1)],
        key=lambda item: item[1],
    )
    frame_paths: list[Path] = []
    target_cursor = 0
    frame_index = 0

    while target_cursor < len(indexed_targets):
        success, frame = capture.read()
        if not success:
            break
        frame_index += 1

        position_ms = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
        if math.isfinite(position_ms) and position_ms > 0:
            current_second = position_ms / 1000.0
        elif fps > 0:
            current_second = frame_index / fps
        else:
            current_second = 0.0

        while target_cursor < len(indexed_targets) and current_second >= indexed_targets[target_cursor][1]:
            target_index, _target_second = indexed_targets[target_cursor]
            frame_path = target_dir / f"frame_{target_index:02d}.png"
            cv2.imwrite(str(frame_path), frame)
            frame_paths.append(frame_path)
            target_cursor += 1

    capture.release()
    frame_paths.sort(key=lambda path: path.name)
    return frame_paths
