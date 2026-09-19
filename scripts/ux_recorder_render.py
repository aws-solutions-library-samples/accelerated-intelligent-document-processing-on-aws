#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
ux_recorder_render.py

Turns a recorded session (frames + timeline + narration text) into a narrated,
captioned mp4. A session is either a UX review (``review.mp4``, end card lists the
report's Findings) or a product demo (``demo.mp4``, end card lists the Key
takeaways); the recorder stamps the kind into the timeline and everything else
below is shared. The pure functions here — pause handling, segmenting,
time remapping, concat lists, SRT cues, Polly text splitting — are what the unit
tests pin; the ffmpeg, Polly and Pillow calls are thin wrappers around them.

Why the video is re-timed rather than played back as recorded
-------------------------------------------------------------
The agent driving the browser thinks between steps, and a screencast of that is
mostly a frozen screen. The screencast only emits frames when something changes,
so idle time shows up as long gaps between consecutive frames. Rendering clamps
every gap to ``gap_max`` and drops paused stretches entirely, then lays each
segment out for a human viewer: hold the boundary frame briefly, start the voice,
let the action land only once the narrator has said what is happening, never
fast-forward past ``max_speedup``, and settle on the final state before the next
segment. A segment's length is therefore driven by its narration and its visible
change, not by how long the model took.

Why the AAC track is 48 kHz stereo
----------------------------------
Polly speaks at 24 kHz mono and the intermediate WAVs keep that, but the final
AAC is resampled to 48 kHz stereo. Apple's decoder treats AAC at 24 kHz or below
as a possible HE-AAC stream with implicit SBR, and QuickTime played the first cut
of the smoke recording silent. 48 kHz is unambiguous everywhere.

Why the subtitle track cannot be "off by default"
-------------------------------------------------
The MP4 muxer enables the first track of each kind whatever disposition is asked
for, so an embedded track is always offered as enabled; whether it is drawn is the
player's choice and its menu toggles it (QuickTime View → Subtitles, VLC Subtitle
menu). ``--no-captions`` leaves the track out; the ``.srt`` is written either way.

Why captions are derived rather than measured
---------------------------------------------
Polly's generative engine does not support speech marks, so there is no per-word
timing to read back. Each narration line's start is known exactly (segment start
plus pre-roll) and its length is measured with ffprobe; sentences share that length
in proportion to their character counts. That is accurate to well under a second,
which is enough for captions that follow the voice.

Why the video is an explicit image sequence
-------------------------------------------
The obvious tool for "show this frame for this long" is the concat demuxer with
``duration`` lines. Measured on ffmpeg 8 it places still images at the wrong times
(a 4 s title card came out at 0.6 s and the whole video ran a second long), so the
render instead lays out one symlink per output frame under ``render/seq/`` and
feeds that to the image2 demuxer at a fixed frame rate. That is deterministic to
the frame: the change at 4.0 s happens at frame 120. Audio still goes through the
concat demuxer, which is reliable for real WAV streams.

Why cards are JPEG images
-------------------------
This machine's ffmpeg is built without libfreetype, so ``drawtext`` is unavailable.
Pillow is already a pinned dependency of the repo, so title and end cards are
rendered as images and placed in the sequence like any other frame. They are JPEG
so the sequence holds a single codec.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

CHAPTERS_BEGIN = "<!-- ux-recorder:chapters -->"
CHAPTERS_END = "<!-- /ux-recorder:chapters -->"
KINDS = ("review", "demo")
DEMO_FOOTER = "GenAI IDP Accelerator"
POLLY_TEXT_LIMIT = 2800
SRT_CUE_GAP = 0.08
SILENT_WARNING_SECONDS = 6.0
WORDS_PER_SECOND_ESTIMATE = 2.6


@dataclass
class Frame:
    seq: int
    t: float
    file: str
    forced: bool = False
    hold: float = 0.0

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Frame:
        return cls(
            seq=int(record["seq"]),
            t=float(record["t"]),
            file=str(record["file"]),
            forced=bool(record.get("forced", False)),
        )


@dataclass
class Event:
    seq: int
    t: float
    type: str
    label: str | None = None
    say: str | None = None
    text: str | None = None
    visible: bool | None = None
    frame: int | None = None
    x: float | None = None
    y: float | None = None
    target: str | None = None
    under: str | None = None
    interactive: str | None = None

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> Event:
        return cls(
            seq=int(record["seq"]),
            t=float(record["t"]),
            type=str(record["type"]),
            label=record.get("label"),
            say=record.get("say"),
            text=record.get("text"),
            visible=record.get("visible"),
            frame=record.get("frame"),
            x=record.get("x"),
            y=record.get("y"),
            target=record.get("target"),
            under=record.get("under"),
            interactive=record.get("interactive"),
        )


@dataclass
class PacingConfig:
    click_hold: float = 0.7
    gap_max: float = 1.5
    pre_roll: float = 0.6
    hold: float = 1.2
    min_seg: float = 2.5
    max_speedup: float = 3.0
    cap: float = 12.0
    fps: int = 30
    card_seconds: float = 4.0


@dataclass
class Segment:
    index: int
    kind: str
    label: str
    files: list[str]
    durations: list[float]
    narration: str | None = None
    narration_dur: float = 0.0
    src_start: float = 0.0
    src_end: float = 0.0
    out_start: float = 0.0
    voice_offset: float = 0.0
    speedup: float = 1.0
    hold_added: float = 0.0
    silent_stretch: float = 0.0
    event_seq: int | None = None

    @property
    def total(self) -> float:
        return sum(self.durations)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["frames"] = len(self.files)
        record["out_dur"] = round(self.total, 3)
        record.pop("files")
        record.pop("durations")
        return record


def pause_intervals(events: Iterable[Event]) -> list[tuple[float, float]]:
    """Closed-open [pause, resume) windows; an unmatched pause ends at stop."""
    ordered = sorted(events, key=lambda e: (e.t, e.seq))
    stop_t = next((e.t for e in ordered if e.type == "stop"), None)
    last_t = ordered[-1].t if ordered else 0.0
    intervals: list[tuple[float, float]] = []
    open_t: float | None = None
    for event in ordered:
        if event.type == "pause" and open_t is None:
            open_t = event.t
        elif event.type == "resume" and open_t is not None:
            if event.t > open_t:
                intervals.append((open_t, event.t))
            open_t = None
    if open_t is not None:
        end = stop_t if stop_t is not None else last_t
        if end > open_t:
            intervals.append((open_t, end))
    return intervals


def apply_pauses(
    events: list[Event], frames: list[Frame]
) -> tuple[list[Event], list[Frame], float]:
    """Drop paused frames and move marks made during a pause to its resume."""
    intervals = pause_intervals(events)

    def containing(t: float) -> tuple[float, float] | None:
        for a, b in intervals:
            if a <= t < b:
                return (a, b)
        return None

    kept_frames = [f for f in frames if containing(f.t) is None]
    moved: list[Event] = []
    for event in events:
        window = containing(event.t)
        if event.type == "mark" and window is not None:
            moved.append(replace(event, t=window[1]))
        else:
            moved.append(event)
    return moved, kept_frames, sum(b - a for a, b in intervals)


def _clamped_durations(
    frames: list[Frame], end_t: float, cfg: PacingConfig
) -> list[float]:
    floor = 1.0 / cfg.fps
    durations: list[float] = []
    for current, following in zip(frames, frames[1:]):
        gap = min(following.t - current.t, cfg.gap_max)
        durations.append(max(gap, current.hold))
    tail = min(max(end_t - frames[-1].t, 0.0), cfg.gap_max)
    durations.append(max(tail, frames[-1].hold))
    return [max(d, floor) for d in durations]


def _footage_segment(
    event: Event,
    frames: list[Frame],
    b0: float,
    b1: float,
    narration: tuple[str, float] | None,
    cfg: PacingConfig,
) -> Segment:
    text, narr_dur = narration if narration else (None, 0.0)
    durations = _clamped_durations(frames, b1, cfg)
    files = [f.file for f in frames]
    lead = min(0.5 * narr_dur, 2.0) if narr_dur else 0.0
    durations[0] = max(durations[0], cfg.pre_roll + lead)
    motion = sum(durations[1:])
    speedup = 1.0
    budget = cfg.cap
    if narr_dur:
        budget = min(cfg.cap, max(narr_dur + cfg.hold, cfg.min_seg))
    if motion > budget:
        motion_target = max(budget, motion / cfg.max_speedup)
        scale = motion_target / motion
        speedup = motion / motion_target
        floor = 1.0 / cfg.fps
        holds = [f.hold for f in frames]
        new_files = [files[0]]
        new_durations = [durations[0]]
        carry = 0.0
        for file, d, held in zip(files[1:], durations[1:], holds[1:]):
            scaled = max(d * scale + carry, held)
            if scaled < floor:
                carry = scaled
                continue
            new_files.append(file)
            new_durations.append(scaled)
            carry = 0.0
        if carry:
            new_durations[-1] += carry
        files, durations = new_files, new_durations
    voice_end = cfg.pre_roll + narr_dur if narr_dur else 0.0
    visual_total = sum(durations)
    total = max(visual_total + cfg.hold, voice_end + cfg.hold, cfg.min_seg)
    hold_added = total - visual_total
    durations[-1] += hold_added
    label = event.label or ("Start" if event.type == "start" else f"Mark {event.seq}")
    return Segment(
        index=0,
        kind="footage",
        label=label,
        files=files,
        durations=durations,
        narration=text,
        narration_dur=narr_dur,
        src_start=b0,
        src_end=b1,
        voice_offset=cfg.pre_roll if narr_dur else 0.0,
        speedup=speedup,
        hold_added=hold_added,
        silent_stretch=total - narr_dur,
        event_seq=event.seq,
    )


def _card_segment(
    kind: str,
    file: str,
    narration: tuple[str, float] | None,
    cfg: PacingConfig,
    end_label: str = "Findings",
) -> Segment:
    text, narr_dur = narration if narration else (None, 0.0)
    base = cfg.card_seconds + (1.0 if kind == "end" else 0.0)
    total = max(base, cfg.pre_roll + narr_dur + cfg.hold)
    return Segment(
        index=0,
        kind=kind,
        label="Title" if kind == "title" else end_label,
        files=[file],
        durations=[total],
        narration=text,
        narration_dur=narr_dur,
        voice_offset=cfg.pre_roll if narr_dur else 0.0,
        silent_stretch=total - narr_dur,
    )


def build_segments(
    events: list[Event],
    frames: list[Frame],
    narration: dict[int, tuple[str, float]],
    cfg: PacingConfig,
    title_file: str | None = None,
    end_file: str | None = None,
    end_label: str = "Findings",
) -> list[Segment]:
    """Lay the recording out as paced segments in output order."""
    ordered = sorted(events, key=lambda e: (e.t, e.seq))
    frames = sorted(frames, key=lambda f: (f.t, f.seq))
    if not frames:
        raise ValueError("no frames were recorded")
    start = next((e for e in ordered if e.type == "start"), None)
    if start is None:
        start = Event(seq=0, t=frames[0].t, type="start", label="Start")
    stop = next((e for e in ordered if e.type == "stop"), None)
    stop_t = stop.t if stop else max(frames[-1].t, start.t)
    marks = [e for e in ordered if e.type == "mark" and e.t <= stop_t]
    bounds: list[tuple[Event, float]] = [(start, start.t)] + [(m, m.t) for m in marks]

    segments: list[Segment] = []
    start_narration = narration.get(start.seq)
    stop_narration = narration.get(stop.seq) if stop else None
    if title_file:
        segments.append(_card_segment("title", title_file, start_narration, cfg))
        start_narration = None

    for i, (event, b0) in enumerate(bounds):
        b1 = bounds[i + 1][1] if i + 1 < len(bounds) else stop_t
        if b1 <= b0:
            b1 = b0 + 1.0 / cfg.fps
        inside = [f for f in frames if b0 <= f.t < b1]
        if not inside:
            before = [f for f in frames if f.t < b0]
            inside = [before[-1]] if before else [frames[0]]
        if event.type == "start":
            line = start_narration
        else:
            line = narration.get(event.seq)
        segments.append(_footage_segment(event, inside, b0, b1, line, cfg))

    if end_file:
        segments.append(
            _card_segment("end", end_file, stop_narration, cfg, end_label=end_label)
        )

    running = 0.0
    for index, segment in enumerate(segments):
        segment.index = index
        segment.out_start = running
        running += segment.total
    return segments


CLICK_FRAME_DIR = "render/clicks"


def click_frames(
    frames: list[Frame],
    events: list[Event],
    hold: float,
    file_for: Callable[[Event, Frame], str | None],
) -> list[Frame]:
    """Insert a held, click-marked copy of the last frame before each click.

    Chrome delivers the frame that follows a mousedown only after the page has
    reacted, and a single-page app reacts within tens of milliseconds, so the
    ripple drawn live lands on the destination page. Compositing the marker onto
    the frame the viewer was looking at when the click happened, and holding it,
    shows the click where it belongs: on the element, before the page changes.
    """
    ordered = sorted(frames, key=lambda f: (f.t, f.seq))
    extra: list[Frame] = []
    next_seq = max((f.seq for f in ordered), default=0) + 1
    for click in sorted((e for e in events if e.type == "click"), key=lambda e: e.t):
        if click.x is None or click.y is None:
            continue
        before = [f for f in ordered if f.t <= click.t and f.hold == 0.0]
        if not before:
            continue
        file = file_for(click, before[-1])
        if not file:
            continue
        extra.append(
            Frame(seq=next_seq, t=click.t + 1e-3, file=file, forced=True, hold=hold)
        )
        next_seq += 1
    return sorted(ordered + extra, key=lambda f: (f.t, f.seq))


def draw_click_marker(
    source: Path, out: Path, x: float, y: float, scale_x: float, scale_y: float
) -> Path:
    """The source frame with a cursor arrow and click ring at the page point."""
    from PIL import Image, ImageDraw

    image = Image.open(source).convert("RGBA")
    px, py = x * scale_x, y * scale_y
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    radius = max(14, int(22 * scale_x))
    draw.ellipse(
        [px - radius, py - radius, px + radius, py + radius],
        fill=(232, 113, 43, 70),
        outline=(232, 113, 43, 230),
        width=max(2, int(3 * scale_x)),
    )
    u = scale_x
    arrow = [
        (px, py),
        (px, py + 22 * u),
        (px + 6 * u, py + 16.5 * u),
        (px + 10.5 * u, py + 26 * u),
        (px + 14.5 * u, py + 24.2 * u),
        (px + 10 * u, py + 14.8 * u),
        (px + 18 * u, py + 14.8 * u),
    ]
    draw.polygon(arrow, fill=(17, 17, 17, 255), outline=(255, 255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.alpha_composite(image, overlay).convert("RGB").save(out, quality=90)
    return out


def _quote(path: str) -> str:
    return "'" + path.replace("'", "'\\''") + "'"


def frame_sequence(segments: list[Segment], fps: int) -> list[str]:
    """The file to show at each output frame, boundaries rounded cumulatively."""
    sequence: list[str] = []
    elapsed = 0.0
    for segment in segments:
        for file, duration in zip(segment.files, segment.durations):
            elapsed += duration
            target = int(round(elapsed * fps))
            while len(sequence) < target:
                sequence.append(file)
    return sequence


def link_sequence(
    sequence: list[str], seq_dir: Path, resolve: Callable[[str], Path]
) -> str:
    """Materialise the sequence as ``seq_dir/000000.jpg…``; returns the ffmpeg pattern."""
    if seq_dir.exists():
        shutil.rmtree(seq_dir)
    seq_dir.mkdir(parents=True)
    for index, file in enumerate(sequence):
        target = resolve(file)
        link = seq_dir / f"{index:06d}.jpg"
        try:
            os.symlink(target, link)
        except OSError:
            try:
                os.link(target, link)
            except OSError:
                shutil.copyfile(target, link)
    return str(seq_dir / "%06d.jpg")


def audio_concat_list(paths: Iterable[str]) -> str:
    lines = ["ffconcat version 1.0"]
    for path in paths:
        lines.append(f"file {_quote(path)}")
    return "\n".join(lines) + "\n"


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    return [p for p in parts if p]


def split_for_polly(text: str, limit: int = POLLY_TEXT_LIMIT) -> list[str]:
    """Sentence-aligned chunks that each fit Polly's per-request text limit."""
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        pieces = [sentence]
        if len(sentence) > limit:
            pieces = []
            words = sentence.split(" ")
            piece = ""
            for word in words:
                candidate = f"{piece} {word}".strip()
                if len(candidate) > limit and piece:
                    pieces.append(piece)
                    piece = word
                else:
                    piece = candidate
            if piece:
                pieces.append(piece)
        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if len(candidate) > limit and current:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def to_ssml(text: str, rate: str = "95%", break_ms: int = 350) -> str:
    sentences = [xml_escape(s) for s in split_sentences(text)]
    body = f'<break time="{break_ms}ms"/>'.join(sentences)
    return f'<speak><prosody rate="{rate}">{body}</prosody></speak>'


def narration_cache_key(
    engine: str, voice: str, text: str, text_type: str = "ssml"
) -> str:
    digest = hashlib.sha256(f"{engine}|{voice}|{text_type}|{text}".encode("utf-8"))
    return digest.hexdigest()[:20]


def estimate_duration(text: str) -> float:
    words = len(text.split())
    return round(words / WORDS_PER_SECOND_ESTIMATE + 0.3, 2) if words else 0.0


def entries_under(markdown: str, heading: str) -> list[str]:
    """The indented entries under the first line starting with ``heading``."""
    entries: list[str] = []
    capturing = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if not capturing:
            if stripped.lower().startswith(heading.lower()):
                capturing = True
            continue
        if not stripped:
            continue
        if not line[0].isspace():
            break
        entries.append(stripped)
    return entries


def findings_from_review(markdown: str) -> list[str]:
    """The indented entries under the 'Findings' heading of a review report."""
    return entries_under(markdown, "findings")


def takeaways_from_demo(markdown: str) -> list[str]:
    """The indented entries under the 'Key takeaways' heading of a demo sheet."""
    return entries_under(markdown, "key takeaways")


def session_kind(meta: dict[str, Any]) -> str:
    kind = str(meta.get("kind") or "review")
    return kind if kind in KINDS else "review"


def report_file(kind: str) -> str:
    return "demo.md" if kind == "demo" else "review.md"


def output_stem(kind: str) -> str:
    return "demo" if kind == "demo" else "review"


def end_card_label(kind: str) -> str:
    return "Takeaways" if kind == "demo" else "Findings"


def _finding_headline(finding: str) -> str:
    """The observation half of a 'what you saw → what to change' finding."""
    head = finding.split(" → ")[0].split(" -> ")[0].strip()
    return head if len(head) <= 160 else head[:157].rstrip() + "…"


def wrap_lines(
    text: str, max_width: float, measure: Callable[[str], float] = len
) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if measure(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def srt_timestamp(seconds: float) -> str:
    total_ms = int(round(max(seconds, 0.0) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def build_srt(segments: list[Segment]) -> str:
    """One cue per sentence, timed by character share of the measured line."""
    cues: list[str] = []
    number = 1
    for segment in segments:
        if not segment.narration or segment.narration_dur <= 0:
            continue
        sentences = split_sentences(segment.narration)
        total_chars = sum(len(s) for s in sentences) or 1
        t = segment.out_start + segment.voice_offset
        for sentence in sentences:
            share = segment.narration_dur * len(sentence) / total_chars
            start = t
            end = max(start + 0.2, t + share - SRT_CUE_GAP)
            end = min(end, t + share) if share > 0.2 else end
            cues.append(
                f"{number}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n{sentence}\n"
            )
            number += 1
            t += share
    return "\n".join(cues)


def click_table(events: list[Event]) -> tuple[str, list[str]]:
    """Where each click landed, and a warning for any that hit nothing interactive."""
    clicks = [e for e in events if e.type == "click"]
    if not clicks:
        return "", []
    start_t = min(e.t for e in events)
    marks = sorted((e for e in events if e.type == "mark"), key=lambda e: e.t)
    rows = [f"{'t':>7} {'x,y':>10}  hit"]
    warnings: list[str] = []
    for click in clicks:
        during = next((m.label for m in reversed(marks) if m.t <= click.t), "Start")
        hit = click.interactive or click.under or click.target or "?"
        rows.append(
            f"{_mmss(click.t - start_t):>7} {f'{int(click.x or 0)},{int(click.y or 0)}':>10}  "
            f"{hit}{'' if click.interactive else '   ⚠️ nothing interactive'}"
        )
        if not click.interactive:
            warnings.append(
                f"click at {_mmss(click.t - start_t)} during '{during}' landed on "
                f"{click.under or click.target or 'nothing'}, not on an interactive element"
            )
    return "\n".join(rows), warnings


def pacing_table(segments: list[Segment], cfg: PacingConfig) -> tuple[str, list[str]]:
    header = f"{'#':>3} {'at':>7} {'kind':7} {'frames':>6} {'voice':>6} {'total':>6} {'x':>4} {'hold':>5}  label"
    rows = [header]
    warnings: list[str] = []
    for s in segments:
        rows.append(
            f"{s.index:>3} {_mmss(s.out_start):>7} {s.kind:7} {len(s.files):>6} "
            f"{s.narration_dur:>6.1f} {s.total:>6.1f} {s.speedup:>4.1f} {s.hold_added:>5.1f}  {s.label}"
        )
        if s.silent_stretch > SILENT_WARNING_SECONDS and s.kind == "footage":
            warnings.append(
                f"segment {s.index} ({s.label}) is silent for {s.silent_stretch:.1f}s; consider a shorter hold or more narration"
            )
        if s.speedup >= cfg.max_speedup - 1e-6:
            warnings.append(
                f"segment {s.index} ({s.label}) hits the {cfg.max_speedup:.0f}x speed ceiling; long spinner or animation"
            )
    return "\n".join(rows), warnings


def _mmss(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def chapters_block(segments: list[Segment]) -> str:
    lines = [CHAPTERS_BEGIN, "```"]
    for segment in segments:
        lines.append(f"{_mmss(segment.out_start)}  {segment.label}")
    lines.append("```")
    lines.append(CHAPTERS_END)
    return "\n".join(lines)


def replace_chapters(markdown: str, block: str) -> str:
    """Insert or replace the chapters block so re-rendering is idempotent."""
    if CHAPTERS_BEGIN in markdown and CHAPTERS_END in markdown:
        head, _, rest = markdown.partition(CHAPTERS_BEGIN)
        _, _, tail = rest.partition(CHAPTERS_END)
        return head + block + tail
    if CHAPTERS_BEGIN in markdown:
        return markdown.replace(CHAPTERS_BEGIN, block, 1)
    return markdown.rstrip("\n") + "\n\nChapters\n" + block + "\n"


_NARRATION_HEADING = re.compile(r"^## (\d+)(?:\s+(.*))?$")


def parse_narration_md(text: str) -> dict[int, str]:
    """``## <seq> <label>`` headings followed by the line to speak."""
    lines: dict[int, str] = {}
    current: int | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            lines[current] = " ".join(" ".join(buffer).split())

    for raw in text.splitlines():
        if raw.startswith("## "):
            match = _NARRATION_HEADING.match(raw.rstrip())
            if not match:
                raise ValueError(
                    f"narration heading must be '## <seq> <label>', got {raw!r}"
                )
            flush()
            current = int(match.group(1))
            buffer = []
        elif current is not None:
            buffer.append(raw)
    flush()
    return lines


def write_narration_md(events: list[Event]) -> str:
    parts = [
        "Narration lines, one per mark. Edit freely, then run render again;",
        "only changed lines are re-synthesized. Keep the '## <seq> <label>' headings.",
        "",
    ]
    for event in sorted(events, key=lambda e: e.seq):
        if event.type not in ("start", "mark", "stop"):
            continue
        label = event.label or event.type.capitalize()
        parts.append(f"## {event.seq} {label}")
        parts.append(event.say or "")
        parts.append("")
    return "\n".join(parts)


def review_skeleton(stack: str, persona: str, date: str, flows: list[str]) -> str:
    flow_note = f" — flows {', '.join(flows)}" if flows else ""
    return (
        f"🖱️  UX review — {stack}, {persona}, {date}{flow_note}\n"
        "\n"
        "Looked at\n"
        "  \n"
        "\n"
        "Findings                                    (ranked; suggestion, not a demand)\n"
        "  \n"
        "\n"
        "Functional breakage\n"
        "  \n"
        "\n"
        "Not covered\n"
        "  \n"
        "\n"
        "Chapters\n"
        f"{CHAPTERS_BEGIN}\n{CHAPTERS_END}\n"
    )


def demo_skeleton(title: str, date: str, lines: list[str]) -> str:
    detail = "".join(f"{line}\n" for line in lines)
    return (
        f"🎬  Demo — {title}, {date}\n"
        f"{detail}"
        "\n"
        "Storyboard\n"
        "  \n"
        "\n"
        "Key takeaways                               (3-5 lines; these become the end card)\n"
        "  \n"
        "\n"
        "Fixtures\n"
        "  \n"
        "\n"
        "Not shown\n"
        "  \n"
        "\n"
        "Chapters\n"
        f"{CHAPTERS_BEGIN}\n{CHAPTERS_END}\n"
    )


def ffmpeg_argv(
    sequence_pattern: str,
    audio_list: str,
    srt: str | None,
    out: str,
    width: int,
    height: int,
    fps: int,
    captions: str = "embed",
) -> list[str]:
    """The single encode: image sequence + concat audio (+ soft subtitles) → mp4."""
    argv = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-i",
        sequence_pattern,
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        audio_list,
    ]
    if srt and captions != "none":
        argv += ["-i", srt, "-map", "0:v", "-map", "1:a", "-map", "2:s"]
    else:
        argv += ["-map", "0:v", "-map", "1:a"]
    argv += [
        "-vf",
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ar",
        "48000",
        "-ac",
        "2",
    ]
    if srt and captions != "none":
        argv += [
            "-c:s",
            "mov_text",
            "-metadata:s:s:0",
            "language=eng",
        ]
    argv += ["-movflags", "+faststart", out]
    return argv


def audio_segment_argv(
    narration_mp3: str | None, total: float, voice_offset: float, out: str
) -> list[str]:
    if narration_mp3:
        delay_ms = int(round(voice_offset * 1000))
        return [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            narration_mp3,
            "-af",
            f"adelay={delay_ms}:all=1,apad=whole_dur={total:.3f}",
            "-t",
            f"{total:.3f}",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            out,
        ]
    return [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=24000:cl=mono",
        "-t",
        f"{total:.3f}",
        "-c:a",
        "pcm_s16le",
        out,
    ]


def probe_duration(path: str | Path) -> float:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out)


def probe_dimensions(path: str | Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    width, height = (int(x) for x in out.split(",")[:2])
    return width, height


def polly_client(region: str) -> Any:
    import boto3
    from botocore.config import Config

    session = boto3.Session(
        profile_name=os.environ.get("AWS_PROFILE") or None, region_name=region
    )
    return session.client(
        "polly", config=Config(retries={"mode": "standard", "max_attempts": 8})
    )


def synthesize(
    text: str,
    out_dir: Path,
    voice: str,
    engine: str = "generative",
    region: str = "us-east-1",
    plain_text: bool = False,
    client: Any = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Narration mp3 for ``text``, from cache when the same line was spoken before."""
    out_dir.mkdir(parents=True, exist_ok=True)
    text_type = "text" if plain_text else "ssml"
    key = narration_cache_key(engine, voice, text, text_type)
    out = out_dir / f"{key}.mp3"
    if out.exists() and out.stat().st_size > 0:
        return out
    client = client or polly_client(region)
    chunks = split_for_polly(text)
    parts: list[Path] = []
    for i, chunk in enumerate(chunks):
        body = chunk if plain_text else to_ssml(chunk)
        log(f"  polly {voice}/{engine}: {len(chunk)} chars")
        response = client.synthesize_speech(
            Engine=engine,
            VoiceId=voice,
            OutputFormat="mp3",
            SampleRate="24000",
            Text=body,
            TextType=text_type,
        )
        data = response["AudioStream"].read()
        if len(chunks) == 1:
            out.write_bytes(data)
            return out
        part = out_dir / f"{key}.part{i}.mp3"
        part.write_bytes(data)
        parts.append(part)
    list_path = out_dir / f"{key}.parts.txt"
    list_path.write_text(
        audio_concat_list(str(p.resolve()) for p in parts), encoding="utf-8"
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(out),
        ],
        check=True,
    )
    for part in parts:
        part.unlink(missing_ok=True)
    list_path.unlink(missing_ok=True)
    return out


FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Amazon-Ember-Medium.ttf",
)

GLYPH_FALLBACKS = {
    "\u2192": "->",
    "\u2190": "<-",
    "\u2705": "[ok]",
    "\u274c": "[x]",
    "\u23ed\ufe0f": "[skip]",
    "\u23ed": "[skip]",
    "\u26a0\ufe0f": "!",
    "\u26a0": "!",
    "\u2139\ufe0f": "i",
    "\u2139": "i",
    "\u2022": "-",
    "\u2014": "-",
}


def _font_can_draw(font: Any, char: str) -> bool:
    try:
        return bytes(font.getmask(char)) != bytes(font.getmask("\uffff"))
    except (OSError, ValueError):
        return False


def drawable_text(text: str, font: Any) -> str:
    """Swap characters the font lacks for ASCII stand-ins so no box glyphs appear."""
    out = text
    for char, replacement in GLYPH_FALLBACKS.items():
        if char in out and not _font_can_draw(font, char.rstrip("\ufe0f")):
            out = out.replace(char, replacement)
    return out


def _load_font(size: int) -> Any:
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def render_card(
    width: int,
    height: int,
    title: str,
    lines: list[str],
    out: Path,
    footer: str | None = None,
    dense: bool = False,
) -> Path:
    """A dark title/end card, JPEG, the same size and codec as the footage."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "#0f1b2d")
    draw = ImageDraw.Draw(image)
    margin = int(width * 0.07)
    title_font = _load_font(max(int(height / 13), 18))
    body_font = _load_font(max(int(height / (36 if dense else 26)), 12))
    accent_top = int(height * 0.16)
    draw.rectangle(
        [margin, accent_top, margin + int(width * 0.06), accent_top + 8], fill="#e8712b"
    )
    y = accent_top + 30
    for line in wrap_lines(
        drawable_text(title, title_font), width - 2 * margin, title_font.getlength
    ):
        draw.text((margin, y), line, fill="white", font=title_font)
        y += int(title_font.size * 1.25)
    y += int(body_font.size * 1.2)
    max_y = height - margin - (body_font.size * 3 if footer else 0)
    for paragraph in lines:
        paragraph = drawable_text(paragraph, body_font)
        for line in wrap_lines(paragraph, width - 2 * margin, body_font.getlength):
            if y + body_font.size > max_y:
                draw.text((margin, y), "…", fill="#c9d1dc", font=body_font)
                y = max_y + 1
                break
            draw.text((margin, y), line, fill="#e6ebf1", font=body_font)
            y += int(body_font.size * 1.45)
        if y > max_y:
            break
        y += int(body_font.size * 0.5)
    if footer:
        draw.text(
            (margin, height - margin - body_font.size),
            drawable_text(footer, body_font),
            fill="#8fa1b8",
            font=body_font,
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out, quality=92)
    return out


@dataclass
class RenderOptions:
    voice: str = "Ruth"
    region: str = "us-east-1"
    engine: str = "generative"
    no_narration: bool = False
    no_cards: bool = False
    plain_text: bool = False
    dry_run: bool = False
    captions: str = "embed"
    pacing: PacingConfig = field(default_factory=PacingConfig)


def load_session(session_dir: Path) -> tuple[dict[str, Any], list[Event], list[Frame]]:
    timeline = json.loads((session_dir / "timeline.json").read_text(encoding="utf-8"))
    events = [Event.from_record(r) for r in timeline.get("events", [])]
    frames_path = session_dir / timeline.get("frames_file", "frames.jsonl")
    frames = [
        Frame.from_record(json.loads(line))
        for line in frames_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return timeline, events, frames


def narration_lines(session_dir: Path, events: list[Event]) -> dict[int, str]:
    lines = {
        e.seq: e.say for e in events if e.type in ("start", "mark", "stop") and e.say
    }
    override_path = session_dir / "narration.md"
    if override_path.exists():
        for seq, text in parse_narration_md(
            override_path.read_text(encoding="utf-8")
        ).items():
            if text:
                lines[seq] = text
            else:
                lines.pop(seq, None)
    return lines


def render_session(
    session_dir: Path,
    opts: RenderOptions,
    log: Callable[[str], None] = print,
    client_factory: Callable[[str], Any] = polly_client,
) -> Path | None:
    """Render the mp4 (plus .srt and segments.json) for a review or demo session."""
    session_dir = Path(session_dir)
    timeline, events, frames = load_session(session_dir)
    events, frames, paused = apply_pauses(events, frames)
    if not frames:
        raise SystemExit("no frames outside paused intervals; nothing to render")
    meta = timeline.get("session", {})
    kind = session_kind(meta)
    stem = output_stem(kind)
    report_path = session_dir / report_file(kind)
    cfg = opts.pacing
    render_dir = session_dir / "render"
    render_dir.mkdir(exist_ok=True)

    lines = {} if opts.no_narration else narration_lines(session_dir, events)
    narration: dict[int, tuple[str, float]] = {}
    audio_files: dict[int, Path] = {}
    if lines:
        client = None if opts.dry_run else client_factory(opts.region)
        for seq, text in lines.items():
            if opts.dry_run:
                key = narration_cache_key(
                    opts.engine, opts.voice, text, "text" if opts.plain_text else "ssml"
                )
                cached = session_dir / "narration" / f"{key}.mp3"
                duration = (
                    probe_duration(cached)
                    if cached.exists()
                    else estimate_duration(text)
                )
            else:
                mp3 = synthesize(
                    text,
                    session_dir / "narration",
                    opts.voice,
                    opts.engine,
                    opts.region,
                    opts.plain_text,
                    client=client,
                    log=log,
                )
                audio_files[seq] = mp3
                duration = probe_duration(mp3)
            narration[seq] = (text, duration)

    width, height = probe_dimensions(session_dir / frames[0].file)
    width -= width % 2
    height -= height % 2

    viewport = meta.get("viewport") or {}
    try:
        scale_x = width / float(viewport.get("w") or width)
        scale_y = height / float(viewport.get("h") or height)
    except (TypeError, ValueError, ZeroDivisionError):
        scale_x = scale_y = 1.0

    def marked_copy(click: Event, frame: Frame) -> str | None:
        out = session_dir / CLICK_FRAME_DIR / f"{click.seq:04d}.jpg"
        try:
            draw_click_marker(
                session_dir / frame.file,
                out,
                float(click.x),
                float(click.y),
                scale_x,
                scale_y,
            )
        except ImportError:
            return None
        return str(out.relative_to(session_dir))

    frames = click_frames(frames, events, cfg.click_hold, marked_copy)

    title_file: str | None = None
    end_file: str | None = None
    if not opts.no_cards:
        try:
            date = datetime.fromtimestamp(
                float(meta.get("started", frames[0].t))
            ).strftime("%Y-%m-%d")
            report_text = (
                report_path.read_text(encoding="utf-8") if report_path.exists() else ""
            )
            if kind == "demo":
                title_lines = list(meta.get("subtitle") or [])
                render_card(
                    width,
                    height,
                    str(meta.get("title") or "Demo"),
                    title_lines,
                    session_dir / "cards" / "title.jpg",
                    footer=DEMO_FOOTER,
                )
                takeaways = takeaways_from_demo(report_text)
                body = [f"• {t}" for t in takeaways[:5]] or [
                    "Key takeaways: see demo.md"
                ]
                render_card(
                    width,
                    height,
                    "Key takeaways",
                    body,
                    session_dir / "cards" / "end.jpg",
                    footer=DEMO_FOOTER,
                    dense=len(body) > 4,
                )
            else:
                flows = meta.get("flows") or []
                title_lines = [f"Persona: {meta.get('persona', '?')}", f"Date: {date}"]
                if flows:
                    title_lines.append("Flows: " + ", ".join(flows))
                render_card(
                    width,
                    height,
                    f"UX review — {meta.get('stack', 'stack')}",
                    title_lines,
                    session_dir / "cards" / "title.jpg",
                    footer="GenAI IDP Accelerator",
                )
                findings = findings_from_review(report_text)
                body = [f"• {_finding_headline(f)}" for f in findings[:8]] or [
                    "Findings: see review.md"
                ]
                render_card(
                    width,
                    height,
                    "Findings"
                    if len(findings) <= 8
                    else f"Findings (first 8 of {len(findings)})",
                    body,
                    session_dir / "cards" / "end.jpg",
                    footer="Suggestions, not demands — details and the rest in review.md",
                    dense=len(body) > 4,
                )

            title_file = "cards/title.jpg"
            end_file = "cards/end.jpg"
        except ImportError:
            log("Pillow not installed; rendering without title/end cards")

    segments = build_segments(
        events,
        frames,
        narration,
        cfg,
        title_file,
        end_file,
        end_label=end_card_label(kind),
    )
    (session_dir / "segments.json").write_text(
        json.dumps(
            {
                "paused_seconds": round(paused, 3),
                "width": width,
                "height": height,
                "total_seconds": round(sum(s.total for s in segments), 3),
                "segments": [s.to_record() for s in segments],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    srt_text = build_srt(segments)
    srt_path = session_dir / f"{stem}.srt"
    srt_path.write_text(srt_text, encoding="utf-8")

    sequence = frame_sequence(segments, cfg.fps)
    (render_dir / "sequence.txt").write_text(
        "\n".join(sequence) + "\n", encoding="utf-8"
    )
    seq_dir = render_dir / "seq"
    if opts.dry_run:
        sequence_pattern = str(seq_dir / "%06d.jpg")
    else:
        sequence_pattern = link_sequence(
            sequence, seq_dir, lambda f: (session_dir / f).resolve()
        )

    wavs: list[str] = []
    audio_cmds: list[list[str]] = []
    for segment in segments:
        wav = render_dir / f"a_{segment.index:03d}.wav"
        mp3 = None
        if segment.narration and not opts.dry_run:
            seq = segment.event_seq
            if segment.kind == "title":
                seq = next((e.seq for e in events if e.type == "start"), None)
            elif segment.kind == "end":
                seq = next((e.seq for e in events if e.type == "stop"), None)
            path = audio_files.get(seq) if seq is not None else None
            mp3 = str(path) if path else None
        audio_cmds.append(
            audio_segment_argv(mp3, segment.total, segment.voice_offset, str(wav))
        )
        wavs.append(str(wav.resolve()))
    audio_list = render_dir / "audio.txt"
    audio_list.write_text(audio_concat_list(wavs), encoding="utf-8")

    out = session_dir / f"{stem}.mp4"
    encode = ffmpeg_argv(
        sequence_pattern,
        str(audio_list),
        str(srt_path) if srt_text.strip() else None,
        str(out),
        width,
        height,
        cfg.fps,
        opts.captions,
    )

    table, warnings = pacing_table(segments, cfg)
    log(table)
    clicks, click_warnings = click_table(events)
    if clicks:
        log(f"clicks ({sum(1 for e in events if e.type == 'click')}):")
        log(clicks)
    for warning in warnings + click_warnings:
        log(f"⚠️  {warning}")
    log(
        f"paused time removed: {paused:.1f}s; output length: {sum(s.total for s in segments):.1f}s "
        f"({len(sequence)} frames at {cfg.fps} fps)"
    )

    if opts.dry_run:
        log("dry run — would run:")
        for cmd in audio_cmds[:1]:
            log(
                "  "
                + " ".join(cmd)
                + ("  (… one per segment)" if len(audio_cmds) > 1 else "")
            )
        log("  " + " ".join(encode))
        return None

    for cmd in audio_cmds:
        subprocess.run(cmd, check=True)
    subprocess.run(encode, check=True)
    shutil.rmtree(seq_dir, ignore_errors=True)

    if report_path.exists():
        report_path.write_text(
            replace_chapters(
                report_path.read_text(encoding="utf-8"), chapters_block(segments)
            ),
            encoding="utf-8",
        )
    log(f"wrote {out}")
    return out


def check_tools() -> dict[str, str | None]:
    """Versions of the external tools render needs, None where missing."""
    found: dict[str, str | None] = {}
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            found[tool] = None
            continue
        out = subprocess.run([tool, "-version"], capture_output=True, text=True).stdout
        found[tool] = out.splitlines()[0] if out else path
    try:
        import boto3

        found["boto3"] = boto3.__version__
    except ImportError:
        found["boto3"] = None
    try:
        import PIL

        found["Pillow"] = PIL.__version__
    except ImportError:
        found["Pillow"] = None
    return found
