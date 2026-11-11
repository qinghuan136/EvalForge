"""跨实例预览 Hub：短期 Redis 快照与即时通知。"""

import json
from dataclasses import dataclass, field

from redis.asyncio import Redis

from .schemas.output_preview import OutputChunk

PREVIEW_TTL_SECONDS = 600
PREVIEW_MAX_BYTES = 256 * 1024
PREVIEW_MAX_CHUNKS = 1024

# 每个片段单独存为 hash 字段，追加时不搬运此前累积的文本。
# 先更新快照，再发布去重后的整批；订阅者可用 last_seq 衔接两者。
_APPEND_PREVIEW = """
local key = KEYS[1]
local old = redis.call('HMGET', key, 'last_seq', 'bytes', 'count', 'has_gap', 'truncated')
local last_seq = tonumber(old[1]) or 0
local stored_bytes = tonumber(old[2]) or 0
local stored_count = tonumber(old[3]) or 0
local has_gap = old[4] == '1'
local truncated = old[5] == '1'
local chunks = cjson.decode(ARGV[1])
local new_chunks = {}
local fields = {}
local max_bytes, max_chunks = tonumber(ARGV[2]), tonumber(ARGV[3])
for _, chunk in ipairs(chunks) do
    if chunk.seq > last_seq then
        has_gap = has_gap or chunk.seq ~= last_seq + 1
        last_seq = chunk.seq
        local size = string.len(chunk.text)
        if not truncated and stored_bytes + size <= max_bytes and stored_count < max_chunks then
            fields[#fields + 1] = 'chunk:' .. tostring(chunk.seq)
            fields[#fields + 1] = chunk.text
            stored_bytes = stored_bytes + size
            stored_count = stored_count + 1
        else
            truncated = true
        end
        new_chunks[#new_chunks + 1] = chunk
    end
end
if #new_chunks == 0 then return 0 end
if #fields > 0 then redis.call('HSET', key, unpack(fields)) end
redis.call('HSET', key, 'last_seq', last_seq, 'bytes', stored_bytes, 'count', stored_count,
           'has_gap', has_gap and '1' or '0', 'truncated', truncated and '1' or '0')
redis.call('EXPIRE', key, ARGV[4])
redis.call('PUBLISH', ARGV[5], cjson.encode({
    kind = 'job.output_chunk', experiment_id = ARGV[6],
    job_id = ARGV[7], attempt_id = ARGV[8], chunks = new_chunks,
    last_seq = last_seq, has_gap = has_gap, truncated = truncated
}))
return #new_chunks
"""


@dataclass(frozen=True, slots=True)
class PreviewTarget:
    experiment_id: int
    job_id: str
    attempt_id: str

    @property
    def key(self) -> tuple[str, str]:
        return self.job_id, self.attempt_id


@dataclass(frozen=True, slots=True)
class PreviewSnapshot:
    chunks: tuple[OutputChunk, ...] = field(repr=False)
    last_seq: int
    has_gap: bool
    truncated: bool


class ExperimentEventStore:
    def __init__(self, redis: Redis):
        self.redis = redis
        self._append = redis.register_script(_APPEND_PREVIEW)

    @staticmethod
    def channel(experiment_id: int) -> str:
        return f"evalforge:experiment:{experiment_id}:events"

    @staticmethod
    def _snapshot_key(target: PreviewTarget) -> str:
        pair = json.dumps(target.key, separators=(",", ":"))
        return f"evalforge:experiment:{target.experiment_id}:preview:{pair}"

    async def append_preview(self, target: PreviewTarget, chunks: list[OutputChunk]) -> None:
        encoded = json.dumps([chunk.model_dump() for chunk in chunks], ensure_ascii=False, separators=(",", ":"))
        await self._append(keys=[self._snapshot_key(target)], args=[
            encoded, PREVIEW_MAX_BYTES, PREVIEW_MAX_CHUNKS, PREVIEW_TTL_SECONDS,
            self.channel(target.experiment_id), target.experiment_id, *target.key,
        ])

    async def read_preview(self, target: PreviewTarget) -> PreviewSnapshot | None:
        """读取一个 Attempt 已收到的预览；Redis 客户端使用 decode_responses。"""
        fields = await self.redis.hgetall(self._snapshot_key(target))
        if not fields:
            return None
        chunks = (OutputChunk(seq=int(name[6:]), text=text) for name, text in fields.items()
                  if name.startswith("chunk:"))
        return PreviewSnapshot(tuple(sorted(chunks, key=lambda chunk: chunk.seq)), int(fields["last_seq"]),
                               fields["has_gap"] == "1", fields["truncated"] == "1")
