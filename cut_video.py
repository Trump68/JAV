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
import shutil
import subprocess
import sys
from pathlib import Path


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
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Using -ss/-to *after* -i gives better accuracy when re-encoding.
    # For -mode copy we still re-use the same layout; it may still land on keyframes.
    cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info", "-i", str(input_path)]

    cmd += ["-ss", str(start_s), "-to", str(end_s)]
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

