"""Agent SSE 分帧及会话内的预览片段缓冲。"""

import asyncio
import re
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass, field

_LINE_END = re.compile(rb"\r\n?|\n")
MAX_CHUNK_CHARS = 2048


async def sse_events(chunks: AsyncIterable[bytes]) -> AsyncIterator[tuple[str, str]]:
    """空行结束一个事件；分帧可跨 HTTP 读取，EOF 丢弃未结束的事件。"""
    line = bytearray()
    data: list[str] = []
    event_type = ""
    first_line = True
    previous_cr = False
    async for chunk in chunks:
        if not chunk:
            continue
        start = int(previous_cr and chunk.startswith(b"\n"))
        previous_cr = chunk.endswith(b"\r")
        for match in _LINE_END.finditer(chunk, start):
            line.extend(chunk[start:match.start()])
            value = line.decode("utf-8-sig" if first_line else "utf-8")
            first_line = False
            line.clear()
            start = match.end()
            if not value:
                event = (event_type or "message", "\n".join(data)) if data else None
                data.clear()
                event_type = ""
                if event is not None:
                    yield event
            else:
                name, _, content = value.partition(":")
                if name == "data":
                    data.append(content.removeprefix(" "))
                elif name == "event":
                    event_type = content.removeprefix(" ")
        line.extend(chunk[start:])


@dataclass(frozen=True, slots=True)
class OutputChunk:
    seq: int
    text: str = field(repr=False)


class OutputAccumulator:
    """累积当前连接待发送的片段；满时丢弃新片段，绝不阻塞 HTTP 读取。"""

    def __init__(self) -> None:
        self._pending: deque[tuple[tuple[str, str], OutputChunk]] = deque()
        self._available = asyncio.Event()

    def offer(self, key: tuple[str, str], chunk: OutputChunk) -> None:
        if len(self._pending) < 64:
            self._pending.append((key, chunk))
            self._available.set()

    async def next_batch(self) -> tuple[tuple[str, str], list[OutputChunk]]:
        await self._available.wait()
        key, first = self._pending.popleft()
        chunks = [first]
        while len(chunks) < 16 and self._pending and self._pending[0][0] == key:
            chunks.append(self._pending.popleft()[1])
        if not self._pending:
            self._available.clear()
        return key, chunks
