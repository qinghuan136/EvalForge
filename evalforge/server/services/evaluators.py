"""List built-in rules, manage dataset bindings, and freeze experiment scoring configuration."""

import re

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from evalforge.db import Database
from evalforge.db.models import BuiltinEvaluator, DatasetEvaluators, Evaluator

from ..errors import APIError
from ..evaluators.builtin import BuiltinEvaluator as BuiltinRule
from ..evaluators.builtin import get_builtin_evaluator, list_builtin_evaluators
from ..schemas.evaluators import (
    BuiltinEvaluatorDefinition, DatasetEvaluatorCreate, DatasetEvaluatorDetail,
    DatasetEvaluatorPage, EvaluatorSnapshot, InputMapping,
)
from .auth import locked_user
from .datasets import require_owned_dataset

_SINGLE_VALUE_PATH = re.compile(r"\$\.(?:input|reference|output|metadata)(?:\.[^\W\d][\w-]*|\[(?:0|[1-9][0-9]*)\])*")


def builtin_catalog() -> list[BuiltinEvaluatorDefinition]:
    return [BuiltinEvaluatorDefinition(key=rule.key, description=rule.description,
                                       input_schema=rule.model_json_schema(), output_configs=rule.output_configs())
            for rule in list_builtin_evaluators()]


def _validate_mapping(mapping: InputMapping, rule: type[BuiltinRule]) -> None:
    paths, literals = mapping.path_mapping, mapping.literal_mapping
    fields = rule.model_fields
    if paths.keys() & literals.keys():
        raise APIError(422, "invalid_input_mapping", "一个评分参数只能配置一种取值方式")
    configured = paths.keys() | literals.keys()
    if configured - fields.keys():
        raise APIError(422, "invalid_input_mapping", "输入映射包含评分器未声明的参数")
    if any(field.is_required() and name not in configured for name, field in fields.items()):
        raise APIError(422, "invalid_input_mapping", "输入映射缺少评分器的必填参数")
    for path in paths.values():
        if len(path) > 1024 or not _SINGLE_VALUE_PATH.fullmatch(path):
            raise APIError(422, "invalid_input_mapping", "评分参数路径必须指向一个明确的值")
    for name, value in literals.items():
        try:
            TypeAdapter(fields[name].annotation).validate_python(value, strict=True)
        except ValidationError:
            raise APIError(422, "invalid_input_mapping", "固定值类型与评分器的参数定义不符") from None


async def _builtin_id(db: AsyncSession, rule: type[BuiltinRule]) -> int:
    query = select(BuiltinEvaluator.id).where(BuiltinEvaluator.key == rule.key)
    existing = await db.scalar(query)
    if existing is not None:
        return existing

    # `evaluators.name` reserves the name atomically when separate users first select the same rule.
    table = Evaluator.__table__
    dialect_insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    new_id = await db.scalar(dialect_insert(table).values(kind="BUILTIN", name=rule.key, description=rule.description)
                             .on_conflict_do_nothing(index_elements=[table.c.name]).returning(table.c.id))
    if new_id is not None:
        await db.execute(insert(BuiltinEvaluator.__table__).values(
            id=new_id, kind="BUILTIN", key=rule.key,
            input_schema=rule.model_json_schema(), output_configs=rule.output_configs(),
        ))
        return new_id

    existing = await db.scalar(query)
    if existing is None:
        raise APIError(409, "builtin_evaluator_name_conflict", "内置评分器名称已被占用")
    return existing


def _dataset_bindings(dataset_id: int):
    return (select(DatasetEvaluators, BuiltinEvaluator)
            .join(BuiltinEvaluator, DatasetEvaluators.evaluator_id == BuiltinEvaluator.id)
            .where(DatasetEvaluators.dataset_id == dataset_id))


def _snapshot(row: DatasetEvaluators, builtin: BuiltinEvaluator) -> EvaluatorSnapshot:
    output_configs = row.output_configs
    if output_configs is None:
        output_configs = [{**config, "name": row.name} for config in builtin.output_configs]
    return EvaluatorSnapshot(dataset_evaluator_id=row.id, evaluator_key=builtin.key, name=row.name,
                             description=row.description, input_mapping=row.input_mapping,
                             output_configs=output_configs)


async def load_evaluator_snapshots(db: AsyncSession, dataset_id: int, ids: list[int]) -> list[EvaluatorSnapshot]:
    if not ids:
        return []
    rows = (await db.execute(_dataset_bindings(dataset_id).where(DatasetEvaluators.id.in_(ids)))).all()
    snapshots = {row.id: _snapshot(row, builtin) for row, builtin in rows}
    if len(snapshots) != len(ids):
        raise APIError(404, "dataset_evaluator_not_found", "数据集评分配置不存在")
    return [snapshots[evaluator_id] for evaluator_id in ids]


class DatasetEvaluatorService:
    def __init__(self, database: Database):
        self.database = database

    async def create(self, owner_id: int, auth_version: int, dataset_id: int,
                     data: DatasetEvaluatorCreate) -> DatasetEvaluatorDetail:
        async with self.database.transaction() as db:
            await locked_user(db, owner_id, auth_version)
            await require_owned_dataset(db, owner_id, dataset_id, lock=True)
            try:
                rule = get_builtin_evaluator(data.evaluator_key)
            except KeyError:
                raise APIError(404, "builtin_evaluator_not_found", "内置评分器不存在") from None
            _validate_mapping(data.input_mapping, rule)

            duplicate = await db.scalar(select(DatasetEvaluators.id).where(
                DatasetEvaluators.dataset_id == dataset_id, DatasetEvaluators.name == data.name,
            ))
            if duplicate is not None:
                raise APIError(409, "dataset_evaluator_name_conflict", "此数据集已有同名评分配置")

            binding = DatasetEvaluators(
                dataset_id=dataset_id, evaluator_id=await _builtin_id(db, rule), user_id=owner_id,
                name=data.name, description=data.description, input_mapping=data.input_mapping.model_dump(mode="json"),
                output_configs=[{**config, "name": data.name} for config in rule.output_configs()],
            )
            db.add(binding)
            await db.flush()
            return DatasetEvaluatorDetail(**data.model_dump(), dataset_id=dataset_id,
                                          dataset_evaluator_id=binding.id, output_configs=binding.output_configs)

    async def list(self, owner_id: int, dataset_id: int, page: int, pagesize: int) -> DatasetEvaluatorPage:
        async with self.database.transaction() as db:
            await require_owned_dataset(db, owner_id, dataset_id)
            query = _dataset_bindings(dataset_id)
            total = await db.scalar(query.with_only_columns(func.count(), maintain_column_froms=True))
            rows = (await db.execute(query.order_by(DatasetEvaluators.id).offset((page - 1) * pagesize)
                                     .limit(pagesize))).all()
            items = [DatasetEvaluatorDetail(**_snapshot(row, builtin).model_dump(), dataset_id=dataset_id)
                     for row, builtin in rows]
            return DatasetEvaluatorPage(items=items, total=total, page=page, pagesize=pagesize)
