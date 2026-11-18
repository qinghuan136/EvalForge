"""Single-process fake persistence. Replace this boundary when database storage is added."""

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field

from pydantic_ai.conversation import Conversation, ConversationTypeAdapter

from ..errors import APIError
from ..schemas.assistant import AssistantAction
from ..schemas.runners import RunnerCreateResponse


@dataclass
class StoredConversation:
    user_id: int
    version: int
    snapshot: dict
    model: str
    page_context: dict | None = None
    status: str = "completed"
    error: dict[str, str] | None = None

    def restore(self) -> Conversation:
        return ConversationTypeAdapter.validate_python(deepcopy(self.snapshot))

    def actions(self) -> list[AssistantAction]:
        requests = self.restore().deferred_tool_requests
        return [AssistantAction(tool_call_id=call.tool_call_id, tool=call.tool_name,
                                arguments=call.args_as_dict()) for call in requests.approvals] if requests else []


@dataclass
class FakeConversationStore:
    records: dict[int, StoredConversation] = field(default_factory=dict, repr=False)
    locks: dict[int, asyncio.Lock] = field(default_factory=dict, repr=False)
    credentials: dict[tuple[int, str], tuple[int, RunnerCreateResponse]] = field(default_factory=dict, repr=False)

    def lock(self, session_id: int) -> asyncio.Lock:
        return self.locks.setdefault(session_id, asyncio.Lock())

    async def load_conversation(self, user_id: int, session_id: int) -> StoredConversation | None:
        row = self.records.get(session_id)
        if row is not None and row.user_id != user_id:
            raise APIError(404, "assistant_session_not_found", "助手会话不存在")
        return deepcopy(row)

    async def save_conversation(self, session_id: int, row: StoredConversation) -> None:
        self.records[session_id] = deepcopy(row)

    def credential_ids(self, user_id: int, session_id: int) -> list[str]:
        return [call_id for (sid, call_id), (owner, _) in self.credentials.items()
                if sid == session_id and owner == user_id]

    def take_credential(self, user_id: int, session_id: int, call_id: str) -> RunnerCreateResponse:
        key = (session_id, call_id)
        item = self.credentials.get(key)
        if item is None or item[0] != user_id:
            raise APIError(404, "assistant_credential_not_found", "凭证不存在或已经领取")
        del self.credentials[key]
        return item[1]
