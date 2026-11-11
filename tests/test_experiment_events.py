import json
import unittest
from unittest.mock import patch

import fakeredis
import fakeredis.aioredis

from evalforge.server.experiment_events import ExperimentEventStore, PreviewTarget
from evalforge.server.schemas.output_preview import OutputChunk


class ExperimentEventStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server = fakeredis.FakeServer()
        writer = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        reader = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
        self.addAsyncCleanup(writer.aclose)
        self.addAsyncCleanup(reader.aclose)
        self.writer, self.reader = ExperimentEventStore(writer), ExperimentEventStore(reader)
        self.target = PreviewTarget(42, "job", "attempt")

    async def test_another_instance_reads_snapshot_and_receives_deduplicated_batches(self):
        async with self.reader.redis.pubsub() as subscription:
            channel = self.reader.channel(self.target.experiment_id)
            await subscription.subscribe(channel)
            self.assertEqual((await subscription.get_message(timeout=1))["type"], "subscribe")
            first = [OutputChunk(seq=1, text="订单"), OutputChunk(seq=3, text="😊")]
            await self.writer.append_preview(self.target, first)
            message = await subscription.get_message(timeout=1)
            self.assertEqual(json.loads(message["data"]), {
                "kind": "job.output_chunk", "experiment_id": "42", "job_id": "job", "attempt_id": "attempt",
                "chunks": [item.model_dump() for item in first], "last_seq": 3, "has_gap": True, "truncated": False,
            })
            snapshot = await self.reader.read_preview(self.target)
            self.assertEqual(snapshot.chunks, tuple(first))
            self.assertTrue(snapshot.has_gap)
            await self.writer.append_preview(self.target, first)
            last = OutputChunk(seq=4, text="end")
            await self.writer.append_preview(self.target, [first[-1], last])
            message = await subscription.get_message(timeout=1)
            self.assertEqual(json.loads(message["data"])["chunks"], [last.model_dump()])
            self.assertIsNone(await self.reader.read_preview(PreviewTarget(42, "job", "retry")))
            self.assertIsNone(await self.reader.read_preview(PreviewTarget(43, "job", "attempt")))

    async def test_capped_snapshot_keeps_prefix_while_live_chunks_continue(self):
        async with self.reader.redis.pubsub() as subscription:
            await subscription.subscribe(self.reader.channel(self.target.experiment_id))
            await subscription.get_message(timeout=1)
            first = OutputChunk(seq=1, text="你好")
            with patch("evalforge.server.experiment_events.PREVIEW_MAX_BYTES", 6):
                await self.writer.append_preview(self.target, [first, OutputChunk(seq=2, text="x")])
                await subscription.get_message(timeout=1)
                await self.writer.append_preview(self.target, [OutputChunk(seq=3, text="live")])
            message = json.loads((await subscription.get_message(timeout=1))["data"])
            self.assertEqual(message["chunks"], [{"seq": 3, "text": "live"}])
            self.assertTrue(message["truncated"])
            snapshot = await self.reader.read_preview(self.target)
            self.assertEqual(snapshot.chunks, (first,))
            self.assertEqual(snapshot.last_seq, 3)
            self.assertTrue(snapshot.truncated)
            self.assertFalse(snapshot.has_gap)
            ttl = await self.reader.redis.ttl(self.reader._snapshot_key(self.target))
            self.assertGreater(ttl, 0)
            self.assertLessEqual(ttl, 600)
