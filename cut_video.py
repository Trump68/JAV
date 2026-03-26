"""
cut_video.py

Slice a video by start/end time using ffmpeg.

Examples:
  python cut_video.py --input "in.m4v" --output "out.mp4" --start "00:02:10" --end "00:05:30"
  python cut_video.py --input "in.m4v" --output "out.mp4" --start 130 --end 330 --mode reencode
  python cut_video.py --input "in.mkv" --output "out.mkv" --start "00:00:00" --end "00:00:30" --mode copy
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
import os


def parse_time_to_seconds(s: str) -> float:
    """
    Accept:
      - seconds as float/integer: "130", "130.5"
      - timestamps: "HH:MM:SS[.ms]", "MM:SS[.ms]", "SS[.ms]"
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty time string")

    # plain seconds
    try:
        if ":" not in s:
            return float(s)
    except ValueError:
        pass

    parts = s.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"Invalid time format: {s!r}")

    parts_f = [float(p) for p in parts]
    if len(parts_f) == 2:
        mm, ss = parts_f
        hh = 0.0
    else:
        hh, mm, ss = parts_f
    return hh * 3600.0 + mm * 60.0 + ss


def ffmpeg_available(ffmpeg: str | None) -> str:
    if ffmpeg:
        return ffmpeg
    which = shutil.which("ffmpeg")
    if not which:
        raise RuntimeError(
            "ffmpeg not found in PATH. Install ffmpeg and retry, "
            "or pass --ffmpeg-path \"C:\\path\\to\\ffmpeg.exe\"."
        )
    return which


def ffprobe_available(ffmpeg_bin: str) -> str:
    """
    Try to locate ffprobe next to ffmpeg binary.
    Falls back to PATH lookup.
    """
    try:
        p = Path(ffmpeg_bin)
        candidate = str(p.parent / "ffprobe.exe")
        if Path(candidate).exists():
            return candidate
    except Exception:
        pass
    which = shutil.which("ffprobe")
    if not which:
        raise RuntimeError("ffprobe not found. It is usually shipped with ffmpeg.")
    return which


def get_media_info(ffprobe_bin: str, input_path: Path) -> dict:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-i",
        str(input_path),
    ]
    p = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not p.stdout:
        return {}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Cut a video segment by start/end time.")
    ap.add_argument("--input", "-i", required=True, help="Input video file.")
    ap.add_argument("--output", "-o", required=True, help="Output video file.")
    ap.add_argument("--start", required=True, help='Start time (e.g. "00:01:23.45" or 83.45).')
    ap.add_argument("--end", required=True, help='End time (e.g. "00:05:00" or 300).')
    ap.add_argument(
        "--mode",
        choices=["reencode", "copy"],
        default="reencode",
        help="reencode = accurate cut; copy = faster but may cut on keyframes.",
    )
    ap.add_argument("--ffmpeg-path", default=None, help="Optional full path to ffmpeg.exe.")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Proceed even if ffprobe suggests the input is not a real video (e.g. PNG 1x1).",
    )
    args = ap.parse_args()

    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 2

    start_s = parse_time_to_seconds(args.start)
    end_s = parse_time_to_seconds(args.end)
    if end_s <= start_s:
        print(f"--end must be > --start (start={start_s}, end={end_s})", file=sys.stderr)
        return 2

    ffmpeg_bin = ffmpeg_available(args.ffmpeg_path)
    ffprobe_bin = ffprobe_available(ffmpeg_bin)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-flight checks to help diagnose "empty output" cases.
    info = get_media_info(ffprobe_bin, input_path)
    duration_s = None
    if info:
        try:
            fmt = info.get("format") or {}
            dur = fmt.get("duration")
            duration_s = float(dur) if dur is not None else None
        except Exception:
            duration_s = None

    v_stream = None
    if info:
        for st in info.get("streams") or []:
            if st.get("codec_type") == "video":
                v_stream = st
                break

    if v_stream:
        codec = v_stream.get("codec_name") or "unknown"
        w = v_stream.get("width")
        h = v_stream.get("height")
        # Warn on the common failure mode from your log: PNG/1x1 "video"
        if codec == "png" or (w == 1 and h == 1):
            print(
                f"[warn] Input looks like PNG stream (codec={codec}, size={w}x{h}). "
                f"Your file may be corrupted or not a real video.",
                file=sys.stderr,
            )
            if not args.force:
                print(
                    "[error] Refusing to cut: input is not a real video stream (PNG/1x1). "
                    "Re-download or provide a correct video file. Use --force to bypass.",
                    file=sys.stderr,
                )
                return 4
        print(
            f"[info] video={codec}, size={v_stream.get('width')}x{v_stream.get('height')}, duration={duration_s}s",
            file=sys.stderr,
        )
    else:
        print("[warn] Could not detect a video stream via ffprobe.", file=sys.stderr)

    if duration_s is not None and start_s >= duration_s:
        print(f"[error] --start ({start_s}s) >= input duration ({duration_s}s).", file=sys.stderr)
        return 3

    # Using -ss/-t *after* -i gives better accuracy when re-encoding.
    # For -mode copy we still keep the same layout; it may still land on keyframes.
    cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info", "-i", str(input_path)]

    slice_len = end_s - start_s
    cmd += ["-ss", str(start_s), "-t", str(slice_len)]
    cmd += ["-map", "0"]

    if args.mode == "copy":
        cmd += ["-c", "copy"]
    else:
        # Generic reencode. Keeps things broadly compatible.
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
        ]

    # Helpful for streaming-friendly MP4/MOV outputs.
    if output_path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        cmd += ["-movflags", "+faststart"]

    cmd += [str(output_path)]

    print("Running:", " ".join(cmd), file=sys.stderr)
    try:
        p = subprocess.run(cmd, check=False)
        return 0 if p.returncode == 0 else p.returncode
    except FileNotFoundError:
        print(f"ffmpeg executable not found: {ffmpeg_bin}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())

