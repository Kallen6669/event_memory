"""保留不可变原文位置，阅读批次只作为派生视图。"""

import hashlib
import re
from dataclasses import asdict, dataclass

from .errors import MemoryError

STAMP = r"\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?"
ROW = re.compile(rf"^\[({STAMP})\s*[–—-]\s*({STAMP})\]\s*([^:：]+)[:：]\s?(.*)$")


def clock_ms(value: str) -> int:
    hours, minutes, seconds = value.split(":")
    if int(hours) > 23 or int(minutes) > 59 or float(seconds) >= 60:
        raise MemoryError("invalid_time", "原文含非法日内时刻。")
    whole, _, fraction = seconds.partition(".")
    return ((int(hours) * 60 + int(minutes)) * 60 + int(whole)) * 1000 + int(
        fraction.ljust(3, "0") or 0
    )


@dataclass(frozen=True)
class Segment:
    ref: str
    start_ms: int
    end_ms: int
    speakers: tuple[str, ...]
    text: str
    line: int
    char_start: int
    char_end: int
    kind: str

    def payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Transcript:
    raw: str
    content_hash: str
    segments: tuple[Segment, ...]
    candidate: bool


def parse_transcript(raw: str) -> Transcript:
    segments = []
    offset = 0
    for line_number, original in enumerate(raw.splitlines(keepends=True), 1):
        line = original.rstrip("\r\n")
        match = ROW.fullmatch(line)
        if match:
            start, end, speaker, text = match.groups()
            start_ms, end_ms = clock_ms(start), clock_ms(end)
            if end_ms < start_ms:
                raise MemoryError("invalid_time", f"第 {line_number} 行结束早于开始；跨日须由上游明确。")
            kind = "acoustic" if speaker.startswith("EVENT/") else "speech"
            segments.append(Segment(
                ref=f"U{len(segments) + 1:06d}", start_ms=start_ms, end_ms=end_ms,
                speakers=tuple(speaker.split("+")), text=text, line=line_number,
                char_start=offset + match.start(4), char_end=offset + match.end(4), kind=kind,
            ))
        elif line.lstrip().startswith("["):
            raise MemoryError("invalid_transcript", f"第 {line_number} 行无法解析，未静默丢弃。")
        offset += len(original)
    candidate = any("候选" in line for line in raw.splitlines()[:10])
    return Transcript(raw, hashlib.sha256(raw.encode("utf-8")).hexdigest(), tuple(segments), candidate)


def build_batches(segments: tuple[Segment, ...], max_chars: int = 6000,
                  context_chars: int = 900) -> list[dict]:
    if max_chars < 100 or context_chars < 0 or context_chars >= max_chars:
        raise MemoryError("invalid_batch", "批次容量至少 100 字符，前置上下文须小于批次容量。")
    batches = []
    current = []
    size = 0
    for segment in segments:
        if segment.kind != "speech":
            continue
        weight = len(segment.text) + 75
        if current and size + weight > max_chars:
            batches.append(current)
            current, size = [], 0
        current.append(segment)
        size += weight
    if current:
        batches.append(current)
    result, preceding = [], []
    for primary in batches:
        context, remaining = [], context_chars
        for item in reversed(preceding):
            if len(item.text) + 75 > remaining:
                break
            context.insert(0, item)
            remaining -= len(item.text) + 75
        result.append({"primary": [s.payload() for s in primary],
                       "context": [s.payload() for s in context]})
        preceding = primary
    return result
