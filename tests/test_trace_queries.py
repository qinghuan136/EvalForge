from sqlalchemy import select

from api_support import APITestCase
from evalforge.db.models import Span, Trace, User

MAIN = "ab" * 16
ORPHAN = "cd" * 16
PRIVATE = "ef" * 16
ROOT = "10" * 8
CHILD = "20" * 8
PARENT_MISSING = "30" * 8
CI = {"key": "custom.int", "value": {"intValue": "9007199254740993"}}


class TraceQueryTests(APITestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.register()
        await self.login()
        owner_id = (await self.user()).id
        async with self.database.transaction() as db:
            other = User(username="other", password_hash="test")
            db.add(other)
            await db.flush()
            main = Trace(owner_user_id=owner_id, trace_id=MAIN, start_time_unix_nano=100, end_time_unix_nano=500)
            orphan = Trace(owner_user_id=owner_id, trace_id=ORPHAN, start_time_unix_nano=700, end_time_unix_nano=800)
            same_id = Trace(owner_user_id=other.id, trace_id=MAIN, start_time_unix_nano=200, end_time_unix_nano=300)
            private = Trace(owner_user_id=other.id, trace_id=PRIVATE, start_time_unix_nano=900, end_time_unix_nano=1000)
            db.add_all([
                Span(trace=main, span_id=ROOT, name="POST /ask", kind=2, status_code=1,
                     start_time_unix_nano=100, end_time_unix_nano=500, resource={
                         "schemaUrl": "https://example.test/1",
                         "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "Agent"}}]},
                     }),
                Span(trace=main, span_id=CHILD, parent_span_id=ROOT, name="retrieve", kind=1,
                     start_time_unix_nano=100, end_time_unix_nano=200, status_code=2, status_message="tool unavailable",
                     attributes=[CI], events=[{"name": "finish", "timeUnixNano": "200"}],
                     links=[{"traceId": PRIVATE, "spanId": ROOT}]),
                Span(trace=orphan, span_id=CHILD, parent_span_id=PARENT_MISSING, name="orphan", kind=1,
                     start_time_unix_nano=700, end_time_unix_nano=800),
                Span(trace=same_id, span_id=ROOT, name="other user's span", kind=1,
                     start_time_unix_nano=200, end_time_unix_nano=300),
                Span(trace=private, span_id=ROOT, name="private", kind=1,
                     start_time_unix_nano=900, end_time_unix_nano=1000),
            ])

    async def test_list_is_paginated_scoped_and_updates_title_when_parent_arrives(self):
        response = await self.client.get("/api/v1/traces?pagesize=1")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        data = response.json()
        self.assertEqual((data["page"], data["total"]), (1, 2))
        self.assertEqual((data["items"][0]["title"], data["items"][0]["root_span_id"]), ("orphan", None))
        second = (await self.client.get("/api/v1/traces?page=2&pagesize=1")).json()["items"][0]
        self.assertEqual((second["trace_id"], second["title"], second["service_name"]), (MAIN, "POST /ask", "Agent"))
        for trace_id, expected in ((MAIN, 1), (PRIVATE, 0)):
            self.assertEqual((await self.client.get(f"/api/v1/traces?trace_id={trace_id}")).json()["total"], expected)

        async with self.database.transaction() as db:
            trace = await db.scalar(select(Trace).where(Trace.trace_id == ORPHAN))
            db.add(Span(trace=trace, span_id=PARENT_MISSING, name="late root", kind=2,
                        start_time_unix_nano=700, end_time_unix_nano=800))
        current = (await self.client.get(f"/api/v1/traces?trace_id={ORPHAN}")).json()["items"][0]
        self.assertEqual((current["title"], current["root_span_id"]), ("late root", PARENT_MISSING))

    async def test_detail_paginates_stable_ties_and_span_detail_preserves_raw_fields(self):
        path = f"/api/v1/traces/{MAIN}"
        response = await self.client.get(path + "?limit=1")
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        first, = detail["spans"]
        self.assertEqual((first["span_id"], first["start_time_unix_nano"], detail["next_span_id"]), (ROOT, "100", ROOT))
        self.assertNotIn("attributes", first)
        second = (await self.client.get(path + f"?after_span_id={ROOT}&limit=1")).json()
        self.assertEqual(second["spans"][0]["parent_span_id"], ROOT)
        self.assertEqual(second["spans"][0]["span_id"], CHILD)
        self.assertIsNone(second["next_span_id"])
        span = (await self.client.get(path + f"/spans/{CHILD}")).json()
        self.assertEqual(span["attributes"], [CI])
        self.assertEqual(span["events"][0]["timeUnixNano"], "200")
        self.assertEqual(span["links"][0]["traceId"], PRIVATE)
        self.assertEqual(span["status_message"], "tool unavailable")
        self.assertEqual((await self.client.get(path + f"?after_span_id={PARENT_MISSING}")).status_code, 404)

    async def test_queries_require_login_and_do_not_disclose_other_accounts(self):
        anon = self.new_client()
        self.assertEqual((await anon.get("/api/v1/traces")).status_code, 401)
        for path in (f"/api/v1/traces/{PRIVATE}", f"/api/v1/traces/{PRIVATE}/spans/{ROOT}",
                     f"/api/v1/traces/{ORPHAN}/spans/{ROOT}"):
            self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/traces/invalid")).status_code, 422)
