"""Runner 上报的预览通知。上限与 Runner 的分片、合批规则一致。"""

from typing import Annotated

from pydantic import Field, field_validator, model_validator

from .attempts import AttemptKey, Message


class OutputChunk(Message):
    seq: Annotated[int, Field(ge=1, le=2**31 - 1)]
    text: Annotated[str, Field(min_length=1, max_length=2048, repr=False)]

    @field_validator("text")
    @classmethod
    def valid_utf8(cls, text: str) -> str:
        text.encode("utf-8")
        return text


class OutputPreviewBatch(AttemptKey):
    chunks: Annotated[list[OutputChunk], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def ordered_chunks(self):
        if any(left.seq >= right.seq for left, right in zip(self.chunks, self.chunks[1:])):
            raise ValueError("Chunk sequences must strictly increase within a batch")
        return self
