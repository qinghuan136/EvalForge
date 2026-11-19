from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StringConstraints, model_validator

from .common import PositiveDbId, StoredText
from .page_context import ExperimentPageContext, PageContext

MessageText = Annotated[StoredText, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
ConversationId = Annotated[StoredText, Field(strict=True, min_length=1, max_length=128)]
Revision = Annotated[StoredText, Field(strict=True, min_length=1, max_length=128)]


class AssistantSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[StoredText, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] = "新对话"


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_context: PageContext | None = None
    experiment_id: PositiveDbId | None = Field(default=None, json_schema_extra={"deprecated": True})
    expected_revision: Revision
    message: MessageText
    model_name: Annotated[StoredText, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def normalize_context(self) -> Self:
        if self.experiment_id is not None:
            legacy = ExperimentPageContext(type="experiment", id=self.experiment_id)
            if self.page_context is not None and self.page_context != legacy:
                raise ValueError("experiment_id conflicts with page_context")
            self.page_context = legacy
        return self


class AssistantSessionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    conversation_id: str
    title: str
    revision: str
    created_at: datetime
    updated_at: datetime


class AssistantSessionList(BaseModel):
    items: list[AssistantSessionSummary]
    models: list[str]


class AssistantMessageView(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    text: str
    model_provider: str | None
    model_name: str | None
    source_experiment_id: int | None
    page_context: PageContext | None = None
    created_at: datetime


class AssistantConversationView(BaseModel):
    """Display-only native Conversation structure; never an execution checkpoint."""

    conversation_id: str
    revision: str | None
    usage: dict[str, JsonValue]
    messages: list[dict[str, JsonValue]]
    deferred_tool_requests: dict[str, JsonValue] | None


class AssistantSessionDetail(AssistantSessionSummary):
    messages: list[AssistantMessageView]
    conversation: AssistantConversationView
    status: str = "completed"
    actions: list[AssistantAction] = Field(default_factory=list)
    error: dict[str, str] | None = None


class AssistantAction(BaseModel):
    tool_call_id: str
    tool: str
    title: Literal["批准请求"] = "批准请求"
    description: str
    arguments: dict[str, JsonValue]


class AssistantDisplayField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1)
    value: str
    sensitive: bool = False


class AssistantDisplayCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    tool_call_id: str
    title: str = Field(min_length=1)
    description: str
    fields: list[AssistantDisplayField] = Field(min_length=1)


class AssistantApprovalRequired(BaseModel):
    revision: str
    cards: list[AssistantAction]


class AssistantToolEvent(BaseModel):
    conversation_id: str
    run_id: str
    part: dict[str, JsonValue]


class AssistantToolCallEvent(AssistantToolEvent):
    args_valid: bool | None


class AssistantToolDeferredEvent(BaseModel):
    conversation_id: str
    run_id: str
    requests: dict[str, JsonValue]


class AssistantDone(BaseModel):
    revision: str
    status: Literal["completed", "awaiting_approval"]


class AssistantChatResponse(BaseModel):
    revision: str
    messages: list[AssistantMessageView]
    actions: list[AssistantAction] = Field(default_factory=list)
    status: Literal["completed", "awaiting_approval"] = "completed"


class AssistantMessageRequest(AssistantChatRequest):
    type: Literal["message"]
    conversation_id: ConversationId


class AssistantDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["decision"]
    conversation_id: ConversationId
    expected_revision: Revision
    decisions: dict[str, StrictBool] = Field(min_length=1, max_length=100)


AssistantStreamRequest = Annotated[AssistantMessageRequest | AssistantDecisionRequest, Field(discriminator="type")]
