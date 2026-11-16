"""Typed references to the resources visible when a message is sent."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .common import PositiveDbId, StoredText

ResourceId = Annotated[StoredText, Field(strict=True, min_length=1, max_length=128, pattern=r"\S")]


class _Context(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExperimentPageContext(_Context):
    type: Literal["experiment"]
    id: PositiveDbId


class JobPageContext(_Context):
    type: Literal["job"]
    experiment_id: PositiveDbId
    job_id: ResourceId


class DatasetPageContext(_Context):
    type: Literal["dataset"]
    id: PositiveDbId
    version_id: PositiveDbId | None = None


class RunnerPageContext(_Context):
    type: Literal["runner"]
    id: ResourceId


PageContext = Annotated[
    ExperimentPageContext | JobPageContext | DatasetPageContext | RunnerPageContext,
    Field(discriminator="type"),
]


class PageContextCatalog:
    @staticmethod
    def list_types() -> tuple[str, ...]:
        schema = TypeAdapter(PageContext).json_schema()
        return tuple(schema["discriminator"]["mapping"])
