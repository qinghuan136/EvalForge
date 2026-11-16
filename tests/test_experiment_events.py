import asyncio
import json
import unittest

from evalforge.server.experiment_events import JobPreview, PREVIEW_MAX_BYTES
from evalforge.server.schemas.output_preview import OutputChunk


def payload(frame: str) -> tuple[str, dict]:
    event, data, *_ = frame.splitlines()
    return event.removeprefix("event: "), json.loads(data.removeprefix("data: "))


class JobPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_readers_snapshot_delta_and_close(self):
        preview = JobPreview("job", "attempt")
        preview.append([OutputChunk(seq=1, text="甲")])
        first, second = preview.events(), preview.events()
        self.assertEqual(payload(await anext(first)), ("snapshot", {
            "attempt_id": "attempt", "text": "甲", "truncated": False, "has_gap": False,
        }))
        preview.append([OutputChunk(seq=1, text="ignored"), OutputChunk(seq=3, text="乙🧪")])
        self.assertEqual(payload(await anext(first)), ("delta", {
            "attempt_id": "attempt", "text": "乙🧪", "truncated": False, "has_gap": True,
        }))
        self.assertEqual(payload(await anext(second))[1]["text"], "甲乙🧪")
        waiting = asyncio.ensure_future(anext(second))
        await asyncio.sleep(0)
        preview.close(completed=True)
        self.assertEqual(payload(await asyncio.wait_for(waiting, 1)), ("done", {}))
        await first.aclose()
        await second.aclose()

    async def test_short_contiguous_fragments_use_text_budget_and_slow_reader_resets(self):
        preview = JobPreview("job", "attempt")
        stream = preview.events()
        self.assertEqual(payload(await anext(stream))[1]["text"], "")
        preview.append([OutputChunk(seq=i, text="字") for i in range(1, 2001)])
        self.assertEqual(payload(await anext(stream))[1]["text"], "字" * 2000)
        preview.append([OutputChunk(seq=i, text="🧪" * 2048) for i in range(2001, 2040)])
        event, data = payload(await anext(stream))
        self.assertEqual(event, "snapshot")
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["text"].encode("utf-8")), PREVIEW_MAX_BYTES)
        preview.close()
        self.assertEqual(payload(await anext(stream)), ("interrupted", {}))
        await stream.aclose()
