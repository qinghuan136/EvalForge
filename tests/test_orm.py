"""真实执行内存数据库约束；编译 PostgreSQL DDL，但不连接 PostgreSQL。"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

from evalforge.db.models import (
    Base, BuiltinEvaluator, CodeEvaluator, CodeEvaluatorVersion, Dataset,
    DatasetExample, DatasetExampleRevision, DatasetVersion,
    Evaluator, Experiment, ExperimentConfig, ExperimentDatasetExample, Job,
    JobEvaluation, LLMEvaluator, Runner, Span, Trace, User,
)
from evalforge.models import Dataset as InMemoryDataset, Example

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


class SchemaContractTests(unittest.TestCase):
    def test_columns_and_nullability_match_approved_dictionary(self):
        contract = json.loads(Path(__file__).with_name("schema_contract.json").read_text(encoding="utf-8"))
        self.assertEqual(set(Base.metadata.tables), set(contract))
        for table_name, columns in contract.items():
            with self.subTest(table=table_name):
                actual = {column.name: column.nullable for column in Base.metadata.tables[table_name].columns}
                self.assertEqual(actual, columns)

    def test_all_real_foreign_keys_resolve_within_the_core_schema(self):
        for table in Base.metadata.tables.values():
            for foreign_key in table.foreign_keys:
                self.assertIn(foreign_key.column.table.name, Base.metadata.tables)
                expected = {
                    ("runners", "owner_user_id"): "RESTRICT",
                    ("runner_credentials", "runner_id"): "RESTRICT",
                    ("runner_attempts", "runner_id"): "RESTRICT",
                    ("experiments", "user_id"): "SET NULL",
                    ("experiments", "experiment_config_id"): "NO ACTION",
                    ("experiment_config", "user_id"): "RESTRICT",
                    ("experiment_config", "runner_id"): "RESTRICT",
                    ("experiment_config", "dataset_version_id"): "NO ACTION",
                    ("assistant_conversation_entries", "parent_id"): "RESTRICT",
                    ("assistant_conversation_records", "head_id"): "RESTRICT",
                    ("assistant_conversation_records", "forked_at"): "RESTRICT",
                }.get((table.name, foreign_key.parent.name), "CASCADE")
                self.assertEqual(foreign_key.ondelete, expected)
        self.assertEqual(len(Base.metadata.sorted_tables), 25)
        for name in ("prompts", "projects", "sandbox_configs", "languages"):
            self.assertNotIn(name, Base.metadata.tables)
        self.assertEqual(Dataset.__table__.c.user_id.info["deferred_foreign_key"]["target"], "users.id")
        self.assertFalse(Dataset.__table__.c.user_id.foreign_keys)

    def test_postgresql_ddl_has_jsonb_timestamptz_and_composite_constraints(self):
        configure_mappers()
        dialect = postgresql.dialect()
        sql = "\n".join(str(CreateTable(table).compile(dialect=dialect)) for table in Base.metadata.sorted_tables)
        for table in Base.metadata.tables.values():
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=dialect))
        self.assertIn("JSONB", sql)
        self.assertIn("TIMESTAMP WITH TIME ZONE", sql)
        self.assertIn("FOREIGN KEY(kind, id) REFERENCES evaluators (kind, id)", sql)
        self.assertIn("FOREIGN KEY(experiment_config_id) REFERENCES experiment_config (id)", sql)
        self.assertIn("UNIQUE (config_group_id, version)", sql)
        self.assertNotIn("score", Job.__table__.c)
        self.assertNotIn("evaluator_id", JobEvaluation.__table__.c)

    def test_in_memory_contract_is_still_independent(self):
        dataset = InMemoryDataset("offline", [Example("one", {}, None)])
        self.assertEqual(dataset.examples[0].id, "one")
        self.assertIsNot(InMemoryDataset, Dataset)

    def test_postgresql_preserves_otlp_numeric_and_json_storage(self):
        ddl = str(CreateTable(Span.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("trace_row_id BIGINT", ddl)
        self.assertIn("start_time_unix_nano NUMERIC(20, 0)", ddl)
        self.assertIn("attributes JSON DEFAULT", ddl)
        self.assertNotIn("JSONB", ddl)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def enable_constraints(connection, _):
            connection.execute("PRAGMA foreign_keys = ON")

        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_trace_owner_isolation_and_span_deduplication(self):
        trace_id = "a" * 32
        span_id = "b" * 16
        time_range = {"start_time_unix_nano": 100, "end_time_unix_nano": 200}
        with Session(self.engine) as session, session.begin():
            owners = [User(username=name, password_hash="test") for name in ("alice", "bob")]
            session.add_all(owners)
            session.flush()
            traces = [Trace(owner_user_id=user.id, trace_id=trace_id, **time_range) for user in owners]
            session.add_all(traces)
            session.flush()
            for trace in traces:
                session.add(Span(trace=trace, span_id=span_id, name="request", kind=2, **time_range))
            session.flush()

            with self.assertRaises(IntegrityError), session.begin_nested():
                session.add(Trace(owner_user_id=owners[0].id, trace_id=trace_id, **time_range))
            with self.assertRaises(IntegrityError), session.begin_nested():
                session.add(Span(trace_row_id=traces[0].id, span_id=span_id, name="retry", kind=2, **time_range))

            session.delete(owners[0])
            session.flush()
            self.assertEqual(session.scalars(select(Trace.owner_user_id)).all(), [owners[1].id])
            self.assertEqual(session.scalars(select(Span.trace_row_id)).all(), [traces[1].id])

    def test_span_can_arrive_before_parent_and_keeps_typed_payload(self):
        attributes = [
            {"key": "gen_ai.request.model", "value": {"stringValue": "sample"}},
            {"key": "custom.size", "value": {"intValue": "9007199254740993"}},
        ]
        events = [{"name": "end", "timeUnixNano": "199"}, {"name": "start", "timeUnixNano": "102"}]
        with Session(self.engine) as session, session.begin():
            owner = User(username="alice", password_hash="test")
            session.add(owner)
            session.flush()
            trace = Trace(owner_user_id=owner.id, trace_id="a" * 32,
                          start_time_unix_nano=100, end_time_unix_nano=200)
            span = Span(trace=trace, span_id="b" * 16, parent_span_id="c" * 16, name="request", kind=2,
                        start_time_unix_nano=100, end_time_unix_nano=200, attributes=attributes, events=events)
            session.add(span)
            session.flush()
            span_id = span.id

        with Session(self.engine) as session:
            span = session.get(Span, span_id)
            self.assertEqual(span.parent_span_id, "c" * 16)
            self.assertEqual(span.attributes, attributes)
            self.assertEqual(span.events, events)
            self.assertEqual(span.links, [])

    def seed_experiment(self, session):
        dataset = Dataset(name="prices", metadata_={"purpose": "test"})
        version = DatasetVersion(dataset=dataset, description="initial")
        example = DatasetExample(dataset=dataset, external_id="price-A")
        revision = DatasetExampleRevision(
            dataset_example=example, dataset_version=version, input={"product": "A"},
            output={"price": 100}, revision_kind="CREATE",
        )
        owner = User(username="experiment-owner", password_hash="test-only-hash")
        config = ExperimentConfig(config_group_id=uuid4(), version=1, user=owner, name="baseline",
                                  dataset=dataset, dataset_version=version,
                                  runner=Runner(name="test runner", owner=owner), agent_target_id="agent")
        experiment = Experiment(experiment_config=config, dataset=dataset, dataset_version=version,
                                user=owner, name="baseline", repetitions=1)
        experiment.experiment_dataset_examples.append(ExperimentDatasetExample(
            dataset_example=example, dataset_example_revision=revision,
        ))
        run = Job(
            experiment=experiment, dataset_example=example, repetition_number=1,
            status="succeeded", output={"task_output": None}, start_time=NOW, end_time=NOW + timedelta(milliseconds=150),
        )
        run.evaluations.append(JobEvaluation(
            name="correctness", annotator_kind="CODE", score=0, label="wrong",
            start_time=NOW, end_time=NOW,
        ))
        session.add(experiment)
        session.flush()
        return dataset, version, example, revision, experiment, run

    def test_dataset_taskrun_evalrun_round_trip_and_snapshot_relationship(self):
        with Session(self.engine) as session, session.begin():
            dataset, version, example, revision, experiment, run = self.seed_experiment(session)
            experiment_id = experiment.id
            self.assertAlmostEqual(run.latency_ms, 150)
            self.assertIsNotNone(dataset.created_at)
        with Session(self.engine) as session:
            experiment = session.get(Experiment, experiment_id)
            self.assertEqual(experiment.dataset.name, "prices")
            self.assertEqual(experiment.experiment_dataset_examples[0].dataset_example_revision.output, {"price": 100})
            self.assertEqual(experiment.jobs[0].output, {"task_output": None})
            self.assertEqual(experiment.jobs[0].evaluations[0].score, 0)
            self.assertEqual(experiment.dataset.metadata_, {"purpose": "test"})

    def test_unique_run_and_annotation_keys_are_enforced(self):
        with Session(self.engine) as session, session.begin():
            *_, experiment, run = self.seed_experiment(session)
            experiment_id, example_id, run_id = experiment.id, run.dataset_example_id, run.id
        with Session(self.engine) as session:
            session.add(Job(
                experiment_id=experiment_id, dataset_example_id=example_id, repetition_number=1,
                status="succeeded", output={"task_output": "duplicate"}, start_time=NOW, end_time=NOW,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session:
            session.add(JobEvaluation(
                experiment_run_id=run_id, name="correctness", annotator_kind="CODE", score=1,
                start_time=NOW, end_time=NOW,
            ))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_unique_revision_and_external_identity_are_enforced(self):
        with Session(self.engine) as session, session.begin():
            dataset, version, example, *_ = self.seed_experiment(session)
            dataset_id, version_id, example_id = dataset.id, version.id, example.id
        with Session(self.engine) as session:
            session.add(DatasetExample(dataset_id=dataset_id, external_id="price-A"))
            with self.assertRaises(IntegrityError):
                session.commit()
        with Session(self.engine) as session:
            session.add(DatasetExampleRevision(
                dataset_example_id=example_id, dataset_version_id=version_id,
                input={}, output={}, revision_kind="PATCH",
            ))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_enum_checks_reject_invalid_revision_and_annotation(self):
        with Session(self.engine) as session, session.begin():
            _, version, example, _, _, run = self.seed_experiment(session)
            version_id, example_id, run_id = version.id, example.id, run.id
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(text("UPDATE dataset_example_revisions SET revision_kind='INVALID' WHERE dataset_example_id=:id"), {"id": example_id})
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(text("UPDATE experiment_run_annotations SET annotator_kind='BUILTIN' WHERE experiment_run_id=:id"), {"id": run_id})

    def test_dangling_internal_foreign_key_is_rejected(self):
        with Session(self.engine) as session:
            session.add(DatasetVersion(dataset_id=9999))
            with self.assertRaises(IntegrityError):
                session.commit()

    def test_dataset_delete_cascades_through_snapshots_runs_and_scores(self):
        with Session(self.engine) as session, session.begin():
            dataset, *_ = self.seed_experiment(session)
            dataset_id = dataset.id
        with Session(self.engine) as session, session.begin():
            session.delete(session.get(Dataset, dataset_id))
        with Session(self.engine) as session:
            for model in (Dataset, DatasetVersion, DatasetExample, DatasetExampleRevision,
                          ExperimentConfig, Experiment, ExperimentDatasetExample, Job, JobEvaluation):
                self.assertEqual(session.scalar(select(func.count()).select_from(model)), 0)

    def test_evaluator_joined_inheritance_and_code_versions(self):
        with Session(self.engine) as session, session.begin():
            code = CodeEvaluator(name="code-match", language="PYTHON")
            code.versions.append(CodeEvaluatorVersion(source_code="def evaluate(output): return 1"))
            llm = LLMEvaluator(name="llm-match", prompt_id=1, output_configs=[])
            builtin = BuiltinEvaluator(name="builtin-match", key="exact", input_schema={}, output_configs=[])
            session.add_all([code, llm, builtin])
            session.flush()
            code_id = code.id
        with Session(self.engine) as session:
            evaluators = session.scalars(select(Evaluator).order_by(Evaluator.id)).all()
            self.assertEqual([type(e) for e in evaluators], [CodeEvaluator, LLMEvaluator, BuiltinEvaluator])
            self.assertEqual(evaluators[0].input_mapping, {"literal_mapping": {}, "path_mapping": {}})
            self.assertEqual(evaluators[0].versions[0].source_code, "def evaluate(output): return 1")
            session.delete(session.get(CodeEvaluator, code_id))
            session.commit()
            self.assertIsNone(session.get(Evaluator, code_id))
            self.assertEqual(session.scalar(select(func.count()).select_from(CodeEvaluatorVersion)), 0)

    def test_discriminator_composite_foreign_key_blocks_wrong_subtype(self):
        with Session(self.engine) as session, session.begin():
            code = CodeEvaluator(name="code", language="PYTHON")
            session.add(code)
            session.flush()
            evaluator_id = code.id
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(text("INSERT INTO llm_evaluators(id,kind,prompt_id,output_configs) VALUES (:id,'LLM',1,'[]')"), {"id": evaluator_id})

    def test_runs_pin_a_config_version_and_allow_reuse(self):
        with Session(self.engine) as session, session.begin():
            dataset, version, *_, first, _ = self.seed_experiment(session)
            v1 = first.experiment_config
            v2 = ExperimentConfig(
                config_group_id=v1.config_group_id, version=2, user=v1.user, name="Updated",
                dataset=dataset, runner=v1.runner, agent_target_id="other",
            )
            second = Experiment(experiment_config=v1, dataset=dataset, dataset_version=version,
                                name="Subset", repetitions=1)
            third = Experiment(experiment_config=v2, dataset=dataset, dataset_version=version,
                               name="Changed", repetitions=1)
            session.add_all([second, third])
            session.flush()
            v1_id, v2_id = v1.id, v2.id
            first_id, second_id, third_id = first.id, second.id, third.id
        with Session(self.engine) as session:
            v1, v2 = (session.get(ExperimentConfig, config_id) for config_id in (v1_id, v2_id))
            self.assertEqual({run.id for run in v1.runs}, {first_id, second_id})
            self.assertEqual([run.id for run in v2.runs], [third_id])
            self.assertEqual(v1.agent_target_id, "agent")
            self.assertIsNone(v2.dataset_version_id)
            self.assertIsNotNone(session.get(Experiment, third_id).dataset_version_id)

    def test_config_version_constraints_and_run_reference(self):
        with Session(self.engine) as session, session.begin():
            dataset, version, *_, run, _ = self.seed_experiment(session)
            config = run.experiment_config
            for invalid_version in (0, config.version):
                with self.assertRaises(IntegrityError), session.begin_nested():
                    session.add(ExperimentConfig(
                        config_group_id=config.config_group_id, version=invalid_version,
                        user=config.user, dataset=dataset, runner=config.runner,
                        name="Duplicate or invalid", agent_target_id="agent",
                    ))
                    session.flush()
            with self.assertRaises(IntegrityError), session.begin_nested():
                session.add(Experiment(dataset=dataset, dataset_version=version, name="Missing config", repetitions=1))
                session.flush()
            with self.assertRaises(IntegrityError), session.begin_nested():
                session.delete(config)
                session.flush()


if __name__ == "__main__":
    unittest.main()
