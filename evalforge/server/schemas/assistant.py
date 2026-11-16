from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .common import PositiveDbId, StoredText
from .page_context import ExperimentPageContext, PageContext

MessageText = Annotated[StoredText, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class AssistantSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Annotated[StoredText, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] = "新对话"


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_context: PageContext | None = None
    experiment_id: PositiveDbId | None = Field(default=None, json_schema_extra={"deprecated": True})
    expected_version: Annotated[int, Field(strict=True, ge=0)]
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

    id: int
    title: str
    version: int
    created_at: datetime
    updated_at: datetime


class AssistantSessionList(BaseModel):
    items: list[AssistantSessionSummary]
    models: list[str]


class AssistantMessageView(BaseModel):
    id: int
    role: Literal["user", "assistant"]
    text: str
    model_provider: str | None
    model_name: str | None
    source_experiment_id: int | None
    page_context: PageContext | None = None
    created_at: datetime


class AssistantSessionDetail(AssistantSessionSummary):
    messages: list[AssistantMessageView]


class AssistantChatResponse(BaseModel):
    version: int
    messages: list[AssistantMessageView]
