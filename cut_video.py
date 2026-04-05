"""
cut_video.py

Slice a video by start/end time using ffmpeg.

Examples:
  python cut_video.py --input "in.m4v" --output "out.mp4" --start "00:02:10" --end "00:05:30"
  python cut_video.py --input "in.m4v" --output "out.mp4" --start 130 --end 330 --mode reencode
  python cut_video.py --input "in.mkv" --output "out.mkv" --start "00:00:00" --end "00:00:30" --mode copy
  python cut_video.py -i "in.m4v" -o "highlights.m4v" --task scenes.txt --mode copy
    # also writes highlights_task.txt with STEP times on the output timeline
"""

from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
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


def parse_task_time_to_seconds(s: str) -> float:
    """
    Accept task timestamps:
      - HH.MM.SS.mmm (example: 00.17.48.000)
      - also supports regular parse_time_to_seconds formats
    """
    s = s.strip()
    if not s:
        raise ValueError("Empty task time string")

    # HH.MM.SS.mmm -> HH:MM:SS.mmm
    dot_parts = s.split(".")
    if len(dot_parts) == 4 and all(p.isdigit() for p in dot_parts):
        hh, mm, ss, mmm = dot_parts
        normalized = f"{int(hh):02d}:{int(mm):02d}:{int(ss):02d}.{int(mmm):03d}"
        return parse_time_to_seconds(normalized)

    return parse_time_to_seconds(s)


def parse_task_steps(task_path: Path) -> list[tuple[float, float, str]]:
    """
    Parse lines like:
      STEP=00.17.48.000->00.18.18.000,RUN_1.0,FADE_1
    Returns list of (start_s, end_s, original_line).
    """
    try:
        raw = task_path.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"Cannot read task file: {task_path} ({e})") from e

    steps: list[tuple[float, float, str]] = []
    for line_no, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Optional directives in scene files; not used for cutting yet.
        upper = line.upper()
        if (
            line.startswith("@")
            or upper.startswith("START=")
            or upper.startswith("END=")
            or upper.startswith("BLACK_SCREEN=")
        ):
            continue
        if not line.upper().startswith("STEP="):
            raise ValueError(
                f"{task_path}:{line_no}: expected STEP=... line "
                "(or comment/#, @include, START=..., END=..., BLACK_SCREEN=...)"
            )
        body = line[5:].strip()
        first_field = body.split(",", 1)[0].strip()
        if "->" not in first_field:
            raise ValueError(f"{task_path}:{line_no}: expected STEP=start->end")
        start_raw, end_raw = [x.strip() for x in first_field.split("->", 1)]
        start_s = parse_task_time_to_seconds(start_raw)
        end_s = parse_task_time_to_seconds(end_raw)
        if end_s <= start_s:
            raise ValueError(
                f"{task_path}:{line_no}: end must be > start (start={start_s}, end={end_s})"
            )
        steps.append((start_s, end_s, raw_line))

    if not steps:
        raise ValueError(f"{task_path}: no STEP lines found")
    return steps


def format_task_time_anchor_seconds(s: float) -> str:
    """Map non-negative seconds to HH.MM.SS.mmm (same style as task files)."""
    s = max(0.0, float(s))
    total_ms = int(round(s * 1000))
    ms = total_ms % 1000
    tsec = total_ms // 1000
    sec = tsec % 60
    tmin = tsec // 60
    mm = tmin % 60
    hh = tmin // 60
    if hh > 99:
        hh = hh % 100
    return f"{hh:02d}.{mm:02d}.{sec:02d}.{ms:03d}"


def build_remapped_task_text(task_path: Path, steps: list[tuple[float, float, str]]) -> str:
    """
    Copy task file with STEP= ranges re-timed to the concatenated output timeline.
    Lines START=...,END=... (optional) are rewritten to span 0 -> total duration.
    """
    out_spans: list[tuple[float, float]] = []
    cursor = 0.0
    for start_s, end_s, _ in steps:
        dur = end_s - start_s
        out_spans.append((cursor, cursor + dur))
        cursor += dur
    total_duration_s = cursor

    si = 0
    out_chunks: list[str] = []
    raw = task_path.read_text(encoding="utf-8")
    for raw_line in raw.splitlines(keepends=True):
        if raw_line.endswith("\r\n"):
            nl, body = "\r\n", raw_line[:-2]
        elif raw_line.endswith("\n"):
            nl, body = "\n", raw_line[:-1]
        elif raw_line.endswith("\r"):
            nl, body = "\r", raw_line[:-1]
        else:
            nl, body = "", raw_line

        s = body.strip()
        if not s:
            out_chunks.append(raw_line)
            continue
        if s.startswith("#"):
            out_chunks.append(raw_line)
            continue
        upper = s.upper()
        if (
            s.startswith("@")
            or upper.startswith("BLACK_SCREEN=")
        ):
            out_chunks.append(raw_line)
            continue
        if upper.startswith("START=") and ",END=" in upper:
            out_chunks.append(
                f"START={format_task_time_anchor_seconds(0.0)},"
                f"END={format_task_time_anchor_seconds(total_duration_s)}{nl}"
            )
            continue
        if upper.startswith("START=") or upper.startswith("END="):
            out_chunks.append(raw_line)
            continue
        if not upper.startswith("STEP="):
            out_chunks.append(raw_line)
            continue

        if si >= len(out_spans):
            raise ValueError(f"{task_path}: extra STEP= line during remap (internal error)")
        a, b = out_spans[si]
        si += 1
        rest = body[5:]
        comma_idx = rest.find(",")
        if comma_idx >= 0:
            suffix = rest[comma_idx:]
        else:
            suffix = ""
        out_chunks.append(
            f"STEP={format_task_time_anchor_seconds(a)}->{format_task_time_anchor_seconds(b)}{suffix}{nl}"
        )

    if si != len(out_spans):
        raise ValueError(f"{task_path}: STEP count mismatch during remap (internal error)")
    return "".join(out_chunks)


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


def get_media_info(
    ffprobe_bin: str,
    input_path: Path,
    *,
    force_format: str | None = None,
    probesize: str | None = None,
    analyzeduration: str | None = None,
) -> dict:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
    ]
    if probesize:
        cmd += ["-probesize", probesize]
    if analyzeduration:
        cmd += ["-analyzeduration", analyzeduration]
    if force_format:
        cmd += ["-f", force_format]
    cmd += ["-i", str(input_path)]
    p = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0 or not p.stdout:
        return {}
    try:
        return json.loads(p.stdout)
    except Exception:
        return {}


def _video_streams(info: dict) -> list[dict]:
    return [s for s in (info.get("streams") or []) if s.get("codec_type") == "video"]


def _stream_is_placeholder(st: dict) -> bool:
    c = (st.get("codec_name") or "").lower()
    try:
        w = int(st.get("width") or 0)
        h = int(st.get("height") or 0)
    except (TypeError, ValueError):
        w, h = 0, 0
    return c == "png" or (w <= 1 and h <= 1)


def _audio_stream_count(info: dict) -> int:
    return sum(1 for s in (info.get("streams") or []) if s.get("codec_type") == "audio")


def _build_map_args(info: dict) -> tuple[list[str], dict | None, bool]:
    """
    Map ffmpeg streams. If the first video track is PNG/1x1 but a later video is real H.264/etc.,
    use -map 0:v:N and -map 0:a so cutting uses the real movie.
    Returns (map_args, primary_video_stream_for_ui, skipped_leading_placeholder).
    """
    vids = _video_streams(info)
    if not vids:
        return ["-map", "0"], None, False
    good_i: int | None = None
    for i, s in enumerate(vids):
        if not _stream_is_placeholder(s):
            good_i = i
            break
    if good_i is not None:
        maps = ["-map", f"0:v:{good_i}"]
        if _audio_stream_count(info) > 0:
            maps += ["-map", "0:a"]
        return maps, vids[good_i], good_i > 0
    return ["-map", "0"], vids[0], False


def _guess_demuxer_for_path(path: Path) -> str | None:
    ext = path.suffix.lower()
    if ext in {".mp4", ".m4v", ".mov"}:
        return "mp4"
    if ext in {".mkv", ".mka", ".mks"}:
        return "matroska"
    if ext == ".webm":
        return "webm"
    return None


def _file_magic_hint(path: Path) -> str | None:
    """First bytes: mp4/mov family (ftyp), matroska (EBML), or obvious image junk."""
    try:
        with path.open("rb") as f:
            b = f.read(65536)
    except OSError:
        return None
    if len(b) >= 8 and b[4:8] == b"ftyp":
        return "ftyp"
    if b.startswith(b"\x1a\x45\xdf\xa3"):
        return "ebml"
    if b[:3] == b"GIF" or (len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n"):
        return "image"
    return None


def _stream_png_until_after_iend(f) -> int | None:
    """File position must be immediately after the 8-byte PNG signature. Returns tell() after IEND+CRC."""
    while True:
        hdr = f.read(8)
        if len(hdr) < 8:
            return None
        length = struct.unpack(">I", hdr[:4])[0]
        ctype = hdr[4:8]
        to_skip = length + 4
        while to_skip > 0:
            chunk = f.read(min(to_skip, 64 * 1024 * 1024))
            if not chunk:
                return None
            to_skip -= len(chunk)
        if ctype == b"IEND":
            return f.tell()


def _stream_gif_until_after_trailer(f) -> int | None:
    """File at position 0. Returns byte offset after GIF trailer 0x3B, or None if parse fails."""
    sig = f.read(6)
    if len(sig) < 6 or sig[:3] != b"GIF" or sig[3:6] not in (b"87a", b"89a"):
        return None
    lsd = f.read(7)
    if len(lsd) < 7:
        return None
    packed = lsd[4]
    if packed & 0x80:
        gct = 3 * (2 << (packed & 0x07))
        if f.read(gct) != bytes(gct):
            return None
    while True:
        b1 = f.read(1)
        if not b1:
            return None
        marker = b1[0]
        if marker == 0x3B:
            return f.tell()
        if marker == 0x21:
            if not f.read(1):
                return None
            while True:
                n = f.read(1)
                if not n:
                    return None
                block_len = n[0]
                if block_len == 0:
                    break
                if f.read(block_len) != bytes(block_len):
                    return None
        elif marker == 0x2C:
            if len(f.read(9)) < 9:
                return None
            packed = f.read(1)
            if not packed:
                return None
            p = packed[0]
            if p & 0x80:
                ct = 3 * (2 << (p & 0x07))
                if f.read(ct) != bytes(ct):
                    return None
            while True:
                n = f.read(1)
                if not n:
                    return None
                block_len = n[0]
                if block_len == 0:
                    break
                if f.read(block_len) != bytes(block_len):
                    return None
        else:
            return None


def _iso_bmff_box_start_in_buffer(data: bytes, j: int, fourcc: bytes) -> int | None:
    """j = index where fourcc begins. Returns offset of box start within data, or None."""
    if j < 4 or j + 4 > len(data) or data[j : j + 4] != fourcc:
        return None
    sz = struct.unpack(">I", data[j - 4 : j])[0]
    if sz == 1:
        if j + 12 > len(data):
            return None
        largesize = struct.unpack(">Q", data[j + 4 : j + 12])[0]
        if 16 <= largesize <= 500_000_000:
            return j - 4
        return None
    if 8 <= sz <= 100_000_000:
        return j - 4
    return None


def _scan_iso_bmff_tag(data: bytes, file_offset_base: int, tag: bytes) -> int | None:
    i = 0
    while True:
        j = data.find(tag, i)
        if j == -1:
            return None
        rel = _iso_bmff_box_start_in_buffer(data, j, tag)
        if rel is not None:
            return file_offset_base + rel
        i = j + 1


def _refine_scan_start_skip_junk(path: Path, start: int) -> int:
    """Skip spaces, CR/LF, NUL between image end and next container (common with concat files)."""
    junk = b" \t\r\n\v\0"
    try:
        with path.open("rb") as f:
            f.seek(start)
            while True:
                c = f.read(1)
                if not c:
                    return start
                if c not in junk:
                    return f.tell() - 1
    except OSError:
        return start


def _find_mpegts_sync_offset(data: bytes, base: int) -> int | None:
    """188-byte TS packets starting with sync 0x47."""
    n = len(data)
    if n < 188 * 4:
        return None
    lim = n - 188 * 3
    for i in range(lim):
        if data[i] != 0x47:
            continue
        if data[i + 188] == 0x47 and data[i + 376] == 0x47:
            return base + i
    return None


def _find_h264_annex_b_offset(data: bytes, base: int) -> int | None:
    """First plausible H.264 NAL after 3- or 4-byte start code (SPS/PPS/IDR/slice)."""
    n = len(data)
    i = 0
    while i + 5 <= n:
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            nal_t = data[i + 4] & 0x1F
            if nal_t in (1, 5, 6, 7, 8, 9):
                return base + i
            i += 1
            continue
        if data[i : i + 3] == b"\x00\x00\x01" and i + 4 <= n:
            nal_t = data[i + 3] & 0x1F
            if nal_t in (1, 5, 6, 7, 8, 9):
                return base + i
            i += 1
            continue
        i += 1
    return None


def _find_hevc_annex_b_offset(data: bytes, base: int) -> int | None:
    """HEVC: start code then 2-byte nal header; nal_unit_type in high bits of first byte."""
    n = len(data)
    i = 0
    while i + 6 <= n:
        off = None
        if data[i : i + 4] == b"\x00\x00\x00\x01":
            off = i + 4
        elif data[i : i + 3] == b"\x00\x00\x01":
            off = i + 3
        if off is not None and off + 2 <= n:
            nut = (data[off] >> 1) & 0x3F
            if nut in (19, 20, 32, 33, 34, 1):
                return base + i
        i += 1
    return None


def _search_buffer_for_embedded_stream(
    data: bytes, file_offset_base: int
) -> tuple[int, str] | None:
    """
    Return (absolute_file_offset, ffmpeg_demuxer) for first plausible stream start in buffer.
    demuxer: mp4 (ftyp/moof/styp), mpegts, h264, hevc.
    """
    for tag in (b"ftyp", b"moof", b"styp"):
        hit = _scan_iso_bmff_tag(data, file_offset_base, tag)
        if hit is not None and hit > 0:
            return hit, "mp4"
    ts = _find_mpegts_sync_offset(data, file_offset_base)
    if ts is not None and ts > 0:
        return ts, "mpegts"
    h264 = _find_h264_annex_b_offset(data, file_offset_base)
    if h264 is not None and h264 > 0:
        return h264, "h264"
    hevc = _find_hevc_annex_b_offset(data, file_offset_base)
    if hevc is not None and hevc > 0:
        return hevc, "hevc"
    return None


def _find_embedded_stream_start(
    path: Path, max_scan: int = 512 * 1024 * 1024
) -> tuple[int, str, int] | None:
    """
    After PNG/GIF cover, find start of MP4/fMP4, MPEG-TS, or Annex B H.264/HEVC.
    Returns (offset, ffmpeg_format, scan_start_byte) or None.
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        return None

    raw_after_image = 0
    try:
        with path.open("rb") as f:
            head = f.read(8)
            if head == b"\x89PNG\r\n\x1a\n":
                pos = _stream_png_until_after_iend(f)
                if pos is not None:
                    raw_after_image = pos
                else:
                    f.seek(0)
                    raw_after_image = 0
            elif len(head) >= 6 and head[:3] == b"GIF" and head[3:6] in (b"87a", b"89a"):
                f.seek(0)
                pos = _stream_gif_until_after_trailer(f)
                if pos is not None:
                    raw_after_image = pos
                else:
                    raw_after_image = 0
                    f.seek(0)
            else:
                f.seek(0)
                raw_after_image = 0
    except OSError:
        return None

    if raw_after_image > 0:
        start_scan = _refine_scan_start_skip_junk(path, raw_after_image)
    else:
        start_scan = _refine_scan_start_skip_junk(path, 0)

    span = min(max_scan, max(0, file_size - start_scan))
    if span < 16:
        return None
    try:
        with path.open("rb") as f:
            f.seek(start_scan)
            buf = f.read(span)
    except OSError:
        return None

    found = _search_buffer_for_embedded_stream(buf, start_scan)
    if found is not None:
        off, fmt = found
        if off > 0:
            return off, fmt, start_scan

    if start_scan > 0:
        try:
            with path.open("rb") as f:
                b0 = f.read(min(max_scan, file_size))
        except OSError:
            return None
        found0 = _search_buffer_for_embedded_stream(b0, 0)
        if found0 is not None:
            off0, fmt0 = found0
            if off0 > 0:
                return off0, fmt0, 0
    return None


def _format_exit_code(rc: int) -> str:
    if rc < 0:
        return f"{rc} (unsigned 0x{rc & 0xFFFFFFFF:08X})"
    return str(rc)


def _iter_blind_demux_attempts(input_path: Path) -> list[tuple[str | None, list[str]]]:
    """
    When ffprobe is wrong but the file is huge, try opening without forcing -f first
    (forcing mp4 often yields 'moov atom not found' for non-standard or broken files).
    Each item: (ffmpeg -f value or None for auto, extra args before -i).
    """
    prefix = ["-probesize", "200M", "-analyzeduration", "200M"]
    prefix_big = ["-probesize", "500M", "-analyzeduration", "500M"]
    prefix_huge = ["-probesize", "1G", "-analyzeduration", "1G"]

    magic = _file_magic_hint(input_path)
    attempts: list[tuple[str | None, list[str]]] = []

    if magic == "ebml":
        attempts += [("matroska", prefix), (None, prefix), ("matroska", prefix_big)]
    elif magic == "ftyp":
        # Real MP4/MOV family: auto often works better than -f mp4 alone.
        attempts += [
            (None, prefix),
            ("mov", prefix),
            ("mp4", prefix),
            (None, prefix_big),
            ("mov", prefix_big),
            ("mp4", prefix_big),
            (None, prefix_huge),
        ]
    else:
        # Unknown / image at start / odd — try broad order
        attempts += [
            (None, prefix),
            ("mp4", prefix),
            ("mov", prefix),
            ("matroska", prefix),
            (None, prefix_big),
            ("mp4", prefix_big),
            ("mov", prefix_big),
        ]

    # De-dupe while preserving order
    seen: set[tuple[str | None, tuple[str, ...]]] = set()
    out: list[tuple[str | None, list[str]]] = []
    for fmt, pre in attempts:
        key = (fmt, tuple(pre))
        if key in seen:
            continue
        seen.add(key)
        out.append((fmt, pre))
    return out


def _format_duration(info: dict) -> float | None:
    try:
        fmt = info.get("format") or {}
        dur = fmt.get("duration")
        if dur is not None:
            return float(dur)
    except (TypeError, ValueError):
        pass
    return None


def _has_usable_video(info: dict) -> bool:
    return any(not _stream_is_placeholder(s) for s in _video_streams(info))


def probe_input_for_cut(
    ffprobe_bin: str, input_path: Path
) -> tuple[dict, str | None, list[str]]:
    """
    Run ffprobe with several strategies (larger probe, forced mp4/matroska) so real H.264
    is found when the default probe only sees PNG 1x1 at the start of a large .m4v.

    Returns (info, forced_demuxer or None, extra_ffmpeg_args_before_i).
    """
    guess = _guess_demuxer_for_path(input_path)

    # (force_format, probesize, analyzeduration) — None = omit
    attempts: list[tuple[str | None, str | None, str | None]] = [
        (None, None, None),
        (None, "50M", "50M"),
        (None, "200M", "200M"),
    ]
    if guess:
        attempts += [
            (guess, None, None),
            (guess, "50M", "50M"),
            (guess, "200M", "200M"),
        ]

    last_info: dict = {}
    for ff, ps, ad in attempts:
        inf = get_media_info(ffprobe_bin, input_path, force_format=ff, probesize=ps, analyzeduration=ad)
        if not inf:
            continue
        last_info = inf
        if _has_usable_video(inf):
            extra: list[str] = []
            if ps:
                extra += ["-probesize", ps]
            if ad:
                extra += ["-analyzeduration", ad]
            return inf, ff, extra

    return last_info, None, []


def build_ffmpeg_cut_cmd(
    ffmpeg_bin: str,
    input_path: Path,
    output_path: Path,
    *,
    forced_demuxer: str | None,
    ffmpeg_probe_prefix: list[str],
    map_args: list[str],
    start_s: float,
    slice_len: float,
    mode: str,
) -> list[str]:
    cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info"]
    cmd += ffmpeg_probe_prefix
    if forced_demuxer:
        cmd += ["-f", forced_demuxer]
    cmd += ["-i", str(input_path)]
    cmd += ["-ss", str(start_s), "-t", str(slice_len)]
    cmd += map_args
    if mode == "copy":
        cmd += ["-c", "copy"]
    else:
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
    if output_path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(output_path)]
    return cmd


def build_ffmpeg_cut_cmd_pipe(
    ffmpeg_bin: str,
    output_path: Path,
    *,
    forced_demuxer: str | None,
    ffmpeg_probe_prefix: list[str],
    map_args: list[str],
    start_s: float,
    slice_len: float,
    mode: str,
) -> list[str]:
    """Same as build_ffmpeg_cut_cmd but input is stdin (pipe:0), for byte-offset MP4 after embedded cover."""
    cmd: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "info"]
    cmd += ffmpeg_probe_prefix
    if forced_demuxer:
        cmd += ["-f", forced_demuxer]
    cmd += ["-i", "pipe:0"]
    cmd += ["-ss", str(start_s), "-t", str(slice_len)]
    cmd += map_args
    if mode == "copy":
        cmd += ["-c", "copy"]
    else:
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
    if output_path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
        cmd += ["-movflags", "+faststart"]
    cmd += [str(output_path)]
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser(description="Cut a video segment by start/end time.")
    ap.add_argument("--input", "-i", required=True, help="Input video file.")
    ap.add_argument("--output", "-o", required=True, help="Output video file.")
    ap.add_argument("--start", required=False, help='Start time (e.g. "00:01:23.45" or 83.45).')
    ap.add_argument("--end", required=False, help='End time (e.g. "00:05:00" or 300).')
    ap.add_argument(
        "--task",
        required=False,
        help=(
            "Task file with STEP lines, e.g. "
            'STEP=00.17.48.000->00.18.18.000,RUN_1.0,FADE_1 . '
            "If set, cuts all STEP ranges and concatenates them into --output."
        ),
    )
    ap.add_argument(
        "--task-out",
        default=None,
        metavar="FILE",
        help=(
            "With --task: write a copy of the task file with STEP times re-anchored to the output "
            "(concatenated) timeline. Default: <output_stem>_task.txt next to --output."
        ),
    )
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
        help="Proceed even if ffprobe reports PNG or 1x1 (broken download). Exit code 5 if output is still empty.",
    )
    args = ap.parse_args()

    input_path = Path(args.input).expanduser().resolve()
    output_path = Path(args.output).expanduser()
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 2

    if args.task:
        task_path = Path(args.task).expanduser().resolve()
        if not task_path.exists():
            print(f"Task file not found: {task_path}", file=sys.stderr)
            return 2
        try:
            steps = parse_task_steps(task_path)
        except (RuntimeError, ValueError) as e:
            print(f"[error] {e}", file=sys.stderr)
            return 2

        ffmpeg_bin = ffmpeg_available(args.ffmpeg_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        script_path = Path(__file__).resolve()
        with tempfile.TemporaryDirectory(prefix="cut_video_task_") as td:
            tmp_dir = Path(td)
            part_paths: list[Path] = []
            for idx, (start_s, end_s, src_line) in enumerate(steps, start=1):
                part_path = tmp_dir / f"part_{idx:04d}{output_path.suffix or '.mp4'}"
                cmd = [
                    sys.executable,
                    str(script_path),
                    "--input",
                    str(input_path),
                    "--output",
                    str(part_path),
                    "--start",
                    f"{start_s:.3f}",
                    "--end",
                    f"{end_s:.3f}",
                    "--mode",
                    args.mode,
                ]
                if args.ffmpeg_path:
                    cmd += ["--ffmpeg-path", args.ffmpeg_path]
                if args.force:
                    cmd += ["--force"]
                print(
                    f"[task] STEP {idx}/{len(steps)}: {start_s:.3f}->{end_s:.3f} ({src_line.strip()})",
                    file=sys.stderr,
                )
                p = subprocess.run(cmd, check=False)
                if p.returncode != 0:
                    print(f"[error] STEP {idx} failed with exit code {p.returncode}", file=sys.stderr)
                    return p.returncode
                part_paths.append(part_path)

            concat_list = tmp_dir / "concat_list.txt"
            concat_lines = []
            for pp in part_paths:
                escaped = pp.as_posix().replace("'", r"'\''")
                concat_lines.append(f"file '{escaped}'\n")
            concat_list.write_text("".join(concat_lines), encoding="utf-8")

            concat_cmd = [
                ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "info",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
            ]
            if args.mode == "copy":
                concat_cmd += ["-c", "copy"]
            else:
                concat_cmd += [
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
            if output_path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
                concat_cmd += ["-movflags", "+faststart"]
            concat_cmd += [str(output_path)]
            print("Running:", " ".join(concat_cmd), file=sys.stderr)
            p = subprocess.run(concat_cmd, check=False)
            if p.returncode != 0:
                return p.returncode
            task_out = (
                Path(args.task_out).expanduser().resolve()
                if args.task_out
                else output_path.expanduser().resolve().with_name(
                    f"{output_path.resolve().stem}_task.txt"
                )
            )
            task_out.parent.mkdir(parents=True, exist_ok=True)
            try:
                remapped = build_remapped_task_text(task_path, steps)
                task_out.write_text(remapped, encoding="utf-8")
                print(f"[task] wrote remapped task: {task_out}", file=sys.stderr)
            except OSError as e:
                print(f"[error] could not write remapped task file: {e}", file=sys.stderr)
                return 1
            except ValueError as e:
                print(f"[error] {e}", file=sys.stderr)
                return 1
            return 0

    if not args.start or not args.end:
        print("Either provide --task, or provide both --start and --end.", file=sys.stderr)
        return 2

    start_s = parse_time_to_seconds(args.start)
    end_s = parse_time_to_seconds(args.end)
    if end_s <= start_s:
        print(f"--end must be > --start (start={start_s}, end={end_s})", file=sys.stderr)
        return 2

    ffmpeg_bin = ffmpeg_available(args.ffmpeg_path)
    ffprobe_bin = ffprobe_available(ffmpeg_bin)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-flight: probe with retries (large probesize / forced mp4) when default probe only sees PNG 1x1.
    info, forced_demuxer, ffmpeg_probe_prefix = probe_input_for_cut(ffprobe_bin, input_path)
    if forced_demuxer:
        print(
            f"[info] Using demuxer '{forced_demuxer}' (and/or larger probe) so real video streams are visible.",
            file=sys.stderr,
        )
    elif ffmpeg_probe_prefix:
        print(
            "[info] Using larger -probesize/-analyzeduration so ffprobe/ffmpeg see past a bogus first track.",
            file=sys.stderr,
        )

    duration_s = _format_duration(info)

    map_args, v_stream, skipped_placeholder = _build_map_args(info)

    try:
        file_size = input_path.stat().st_size
    except OSError:
        file_size = 0
    # If ffprobe only sees PNG/1x1 but the file is large, it is often real MP4 with a bogus first track.
    LARGE_FILE_BYTES = 5 * 1024 * 1024

    input_is_placeholder = False
    use_blind_attempts = False
    usable_video = bool(v_stream) and not _stream_is_placeholder(v_stream)

    if usable_video:
        codec = v_stream.get("codec_name") or "unknown"
        if skipped_placeholder:
            print(
                "[info] First video track was PNG/1x1; using a later video stream for the cut.",
                file=sys.stderr,
            )
        print(
            f"[info] video={codec}, size={v_stream.get('width')}x{v_stream.get('height')}, duration={duration_s}s",
            file=sys.stderr,
        )
    elif file_size >= LARGE_FILE_BYTES and not args.force:
        print(
            f"[info] ffprobe only sees PNG/1x1 or no video, but file is {file_size / (1024 * 1024):.1f} MB — "
            "trying ffmpeg with auto-detect / mov / mp4 and larger probes (forced -f mp4 alone often fails).",
            file=sys.stderr,
        )
        use_blind_attempts = True
        map_args = ["-map", "0"]
        input_is_placeholder = True
    elif not args.force:
        if v_stream:
            codec = v_stream.get("codec_name") or "unknown"
            w, h = v_stream.get("width"), v_stream.get("height")
            print(
                f"[warn] No usable video stream after probe (codec={codec}, size={w}x{h}). "
                f"File may be corrupted — re-download or use --force for tiny/broken files.",
                file=sys.stderr,
            )
        else:
            print("[warn] Could not detect a video stream via ffprobe.", file=sys.stderr)
        print(
            "[error] Refusing to cut. Pass --force to try anyway (exit 5 if output empty).",
            file=sys.stderr,
        )
        return 4
    else:
        # --force (small file or user insists)
        input_is_placeholder = True
        if not usable_video and file_size >= LARGE_FILE_BYTES:
            use_blind_attempts = True
            map_args = ["-map", "0"]
            print(
                f"[info] --force: large file ({file_size / (1024 * 1024):.1f} MB) — same blind demux attempts as without --force.",
                file=sys.stderr,
            )
        elif v_stream:
            print(
                f"[warn] --force: cutting despite probe (codec={v_stream.get('codec_name')}, "
                f"{v_stream.get('width')}x{v_stream.get('height')}).",
                file=sys.stderr,
            )
        else:
            print("[warn] --force: no video stream in ffprobe; ffmpeg may still open the container.", file=sys.stderr)

    if duration_s is not None and start_s >= duration_s:
        print(f"[error] --start ({start_s}s) >= input duration ({duration_s}s).", file=sys.stderr)
        return 3

    slice_len = end_s - start_s

    def validate_cut_output() -> tuple[bool, int]:
        """Returns (ok, output_size)."""
        try:
            out_sz = output_path.stat().st_size if output_path.exists() else 0
        except OSError:
            out_sz = 0
        if out_sz < 4096:
            return False, out_sz
        if input_is_placeholder:
            oi = get_media_info(ffprobe_bin, output_path)
            o_v = None
            for st in (oi.get("streams") or []) if oi else []:
                if st.get("codec_type") == "video":
                    o_v = st
                    break
            oc = (o_v or {}).get("codec_name") or ""
            ow = (o_v or {}).get("width")
            oh = (o_v or {}).get("height")
            if oc == "png" or (ow == 1 and oh == 1) or o_v is None:
                return False, out_sz
        return True, out_sz

    try:
        if use_blind_attempts:
            mh = _file_magic_hint(input_path)
            if mh == "image":
                print(
                    "[warn] File begins with GIF/PNG bytes — searching for embedded video "
                    "(MP4/fMP4, MPEG-TS, or H.264/HEVC) after the cover.",
                    file=sys.stderr,
                )
            last_err = ""
            last_code = 1

            cut_hint = _find_embedded_stream_start(input_path)
            if cut_hint is None and mh == "image":
                print(
                    "[info] No ftyp/moof/styp, TS sync, or H.264/HEVC Annex B start in the first 512 MiB "
                    "after PNG/GIF (or from file start if the image could not be skipped). "
                    "Trying whole-file demuxers next.",
                    file=sys.stderr,
                )
            if cut_hint is not None:
                stream_off, stream_fmt, scan_from = cut_hint
                print(
                    f"[info] Embedded stream at byte offset {stream_off} (demux={stream_fmt}, "
                    f"search began at {scan_from}); using pipe:0.",
                    file=sys.stderr,
                )
                if stream_fmt == "mp4":
                    demux_chain: list[tuple[str, list[str]]] = [
                        ("mp4", ["-probesize", "2G", "-analyzeduration", "2G"]),
                        ("mp4", ["-probesize", "500M", "-analyzeduration", "500M"]),
                        ("mov", ["-probesize", "2G", "-analyzeduration", "2G"]),
                    ]
                elif stream_fmt == "mpegts":
                    demux_chain = [
                        ("mpegts", ["-probesize", "200M", "-analyzeduration", "200M"]),
                        ("mpegts", ["-probesize", "500M", "-analyzeduration", "500M"]),
                    ]
                elif stream_fmt == "h264":
                    demux_chain = [
                        ("h264", ["-probesize", "200M", "-analyzeduration", "200M"]),
                        ("hevc", ["-probesize", "200M", "-analyzeduration", "200M"]),
                    ]
                else:
                    demux_chain = [
                        ("hevc", ["-probesize", "200M", "-analyzeduration", "200M"]),
                        ("h264", ["-probesize", "200M", "-analyzeduration", "200M"]),
                    ]

                for fd, pre in demux_chain:
                    fd_label = fd if fd is not None else "auto"
                    cmd = build_ffmpeg_cut_cmd_pipe(
                        ffmpeg_bin,
                        output_path,
                        forced_demuxer=fd,
                        ffmpeg_probe_prefix=pre,
                        map_args=map_args,
                        start_s=start_s,
                        slice_len=slice_len,
                        mode=args.mode,
                    )
                    print("Running:", " ".join(cmd), file=sys.stderr)
                    try:
                        with input_path.open("rb") as inf:
                            inf.seek(stream_off)
                            p = subprocess.run(
                                cmd,
                                check=False,
                                stdin=inf,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                            )
                    except OSError as e:
                        print(f"[warn] Could not read input at offset {stream_off}: {e}", file=sys.stderr)
                        break
                    last_code = p.returncode
                    err_blob = ((p.stderr or "") + "\n" + (p.stdout or "")).strip()
                    last_err = err_blob
                    if p.returncode != 0:
                        print(
                            f"[warn] ffmpeg exit {_format_exit_code(p.returncode)} (pipe from offset, -f {fd_label}).",
                            file=sys.stderr,
                        )
                        continue
                    ok, out_sz = validate_cut_output()
                    if not ok:
                        print(
                            f"[warn] Output invalid or tiny ({out_sz} bytes); trying next pipe demux/probe.",
                            file=sys.stderr,
                        )
                        continue
                    return 0
                print("[warn] Pipe-from-offset attempts failed; falling back to normal path opens.", file=sys.stderr)

            attempts = _iter_blind_demux_attempts(input_path)
            for fd, pre in attempts:
                fd_label = fd if fd is not None else "auto"
                cmd = build_ffmpeg_cut_cmd(
                    ffmpeg_bin,
                    input_path,
                    output_path,
                    forced_demuxer=fd,
                    ffmpeg_probe_prefix=pre,
                    map_args=map_args,
                    start_s=start_s,
                    slice_len=slice_len,
                    mode=args.mode,
                )
                print("Running:", " ".join(cmd), file=sys.stderr)
                p = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                last_code = p.returncode
                err_blob = ((p.stderr or "") + "\n" + (p.stdout or "")).strip()
                last_err = err_blob
                if p.returncode != 0:
                    print(
                        f"[warn] ffmpeg exit {_format_exit_code(p.returncode)} for -f {fd_label}.",
                        file=sys.stderr,
                    )
                    continue
                ok, out_sz = validate_cut_output()
                if not ok:
                    print(
                        f"[warn] Output invalid or tiny ({out_sz} bytes); trying next demuxer/probe.",
                        file=sys.stderr,
                    )
                    continue
                return 0

            print("[error] All blind demux attempts failed. Last ffmpeg output:", file=sys.stderr)
            tail = last_err[-6000:] if len(last_err) > 6000 else last_err
            if tail:
                print(tail, file=sys.stderr)
            el = last_err.lower()
            if "moov atom not found" in el or ("moov" in el and "not found" in el):
                print(
                    "[error] 'moov atom not found' usually means the file is not a valid MP4/MOV, "
                    "or the download stopped before the index (moov) was written — re-download or repair the source.",
                    file=sys.stderr,
                )
            return last_code if last_code != 0 else 5

        cmd = build_ffmpeg_cut_cmd(
            ffmpeg_bin,
            input_path,
            output_path,
            forced_demuxer=forced_demuxer,
            ffmpeg_probe_prefix=ffmpeg_probe_prefix,
            map_args=map_args,
            start_s=start_s,
            slice_len=slice_len,
            mode=args.mode,
        )
        print("Running:", " ".join(cmd), file=sys.stderr)
        p = subprocess.run(cmd, check=False)
        if p.returncode != 0:
            return p.returncode
        ok, out_sz = validate_cut_output()
        if not ok:
            if out_sz < 4096:
                print(
                    f"[error] Output is missing or tiny ({out_sz} bytes). Input is likely not a real video file.",
                    file=sys.stderr,
                )
            else:
                print(
                    "[error] Output is still a placeholder (PNG/1x1 or no video). Re-download the source.",
                    file=sys.stderr,
                )
            return 5
        return 0
    except FileNotFoundError:
        print(f"ffmpeg executable not found: {ffmpeg_bin}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())

