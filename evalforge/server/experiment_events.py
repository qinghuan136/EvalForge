"""Job 预览的进程内缓冲与 SSE 生成器。"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from .schemas.output_preview import OutputChunk

PREVIEW_MAX_BYTES = 256 * 1024
PREVIEW_MAX_SEGMENTS = 1024
PREVIEW_SEGMENT_BYTES = 8 * 1024
HEARTBEAT_SECONDS = 15


@dataclass(slots=True)
class PreviewSegment:
    first_seq: int
    last_seq: int
    text: bytearray = field(repr=False)


def event_frame(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


class JobPreview:
    def __init__(self, job_id: str, attempt_id: str) -> None:
        self.key = job_id, attempt_id
        self._segments: deque[PreviewSegment] = deque()
        self._head = self._tail = self._last_seq = 0
        self._has_gap = False
        self._changed = asyncio.Event()
        self._end: str | None = None

    def append(self, chunks: Sequence[OutputChunk]) -> None:
        if self._end is not None:
            return
        previous_tail = self._tail
        for chunk in chunks:
            if chunk.seq <= self._last_seq:
                continue
            contiguous = chunk.seq == self._last_seq + 1
            self._has_gap |= not contiguous
            self._last_seq = chunk.seq
            encoded = chunk.text.encode("utf-8")
            last = self._segments[-1] if self._segments else None
            if last is not None and contiguous and len(last.text) + len(encoded) <= PREVIEW_SEGMENT_BYTES:
                last.text.extend(encoded)
                last.last_seq = chunk.seq
            else:
                self._segments.append(PreviewSegment(chunk.seq, chunk.seq, bytearray(encoded)))
            self._tail += len(encoded)
        if self._tail == previous_tail:
            return
        while self._tail - self._head > PREVIEW_MAX_BYTES or len(self._segments) > PREVIEW_MAX_SEGMENTS:
            self._head += len(self._segments.popleft().text)
        # 每次写入唤醒所有旧读者。读者取到信号后，即使还没 await，也不会漏掉写入。
        self._changed.set()
        self._changed = asyncio.Event()

    def close(self, *, completed: bool = False) -> None:
        if self._end is None:
            self._end = "done" if completed else "interrupted"
            self._segments.clear()
            self._changed.set()

    def _text_from(self, cursor: int) -> str:
        offset = cursor - self._head
        parts = []
        for segment in self._segments:
            size = len(segment.text)
            if offset < size:
                parts.append(memoryview(segment.text)[offset:])
                offset = 0
            else:
                offset -= size
        return b"".join(parts).decode("utf-8")

    async def events(self) -> AsyncIterator[str]:
        cursor = None
        while self._end is None:
            changed = self._changed
            reset = cursor is None or cursor < self._head
            if reset or cursor < self._tail:
                text = self._text_from(self._head if reset else cursor)
                cursor = self._tail
                yield event_frame("snapshot" if reset else "delta", {
                    "attempt_id": self.key[1], "text": text,
                    "truncated": self._head > 0, "has_gap": self._has_gap,
                })
            else:
                try:
                    await asyncio.wait_for(changed.wait(), HEARTBEAT_SECONDS)
                except TimeoutError:
                    if self._end is None:
                        yield ": keepalive\n\n"
        yield event_frame(self._end, {})
