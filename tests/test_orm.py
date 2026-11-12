"""真实执行内存数据库约束；编译 PostgreSQL DDL，但不连接 PostgreSQL。"""

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

from evalforge.db.models import (
    Base, BuiltinEvaluator, CodeEvaluator, CodeEvaluatorVersion, Dataset,
    DatasetEvaluators, DatasetExample, DatasetExampleRevision, DatasetVersion,
    Evaluator, Experiment, ExperimentDatasetEvaluator, ExperimentDatasetExample,
    ExperimentEvalOnlyConfig, ExperimentConfig, ExperimentPromptTask, Job,
    JobEvaluation, LLMEvaluator,
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
                }.get((table.name, foreign_key.parent.name), "CASCADE")
                self.assertEqual(foreign_key.ondelete, expected)
        self.assertEqual(len(Base.metadata.sorted_tables), 21)
        for name in ("spans", "prompts", "projects", "sandbox_configs", "languages"):
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
        self.assertIn("FOREIGN KEY(type, id) REFERENCES experiment_jobs (type, id)", sql)
        self.assertNotIn("score", Job.__table__.c)
        self.assertNotIn("evaluator_id", JobEvaluation.__table__.c)

    def test_in_memory_contract_is_still_independent(self):
        dataset = InMemoryDataset("offline", [Example("one", {}, None)])
        self.assertEqual(dataset.examples[0].id, "one")
        self.assertIsNot(InMemoryDataset, Dataset)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def enable_constraints(connection, _):
            connection.execute("PRAGMA foreign_keys = ON")

        Base.metadata.create_all(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def seed_experiment(self, session):
        dataset = Dataset(name="prices", metadata_={"purpose": "test"})
        version = DatasetVersion(dataset=dataset, description="initial")
        example = DatasetExample(dataset=dataset, external_id="price-A")
        revision = DatasetExampleRevision(
            dataset_example=example, dataset_version=version, input={"product": "A"},
            output={"price": 100}, revision_kind="CREATE",
        )
        experiment = Experiment(dataset=dataset, dataset_version=version, name="baseline", repetitions=1)
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
                          Experiment, ExperimentDatasetExample, Job, JobEvaluation):
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

    def test_prompt_and_eval_only_jobs_and_evaluator_links(self):
        with Session(self.engine) as session, session.begin():
            dataset, version, *_, experiment, run = self.seed_experiment(session)
            evaluator = BuiltinEvaluator(name="builtin", key="exact", input_schema={}, output_configs=[])
            configuration = DatasetEvaluators(dataset=dataset, evaluator=evaluator, name="match", input_mapping={"path_mapping": {}, "literal_mapping": {}}, project_id=1)
            prompt = ExperimentPromptTask(
                experiment=experiment, model_provider="OPENAI", model_name="example-model",
                template_type="CHAT", template_format="MUSTACHE", template={"messages": []},
            )
            prompt.dataset_evaluator_links.append(ExperimentDatasetEvaluator(dataset_evaluator=configuration))
            eval_experiment = Experiment(dataset=dataset, dataset_version=version, name="eval-only", repetitions=1)
            eval_job = ExperimentEvalOnlyConfig(experiment=eval_experiment)
            session.add_all([prompt, eval_job])
            session.flush()
            prompt_id, eval_id = prompt.id, eval_job.id
            self.assertEqual(prompt.status, "STOPPED")
            self.assertEqual(prompt.max_concurrency, 10)
        with Session(self.engine) as session:
            self.assertIsInstance(session.get(ExperimentConfig, prompt_id), ExperimentPromptTask)
            self.assertIsInstance(session.get(ExperimentConfig, eval_id), ExperimentEvalOnlyConfig)
            self.assertEqual(session.get(ExperimentConfig, prompt_id).dataset_evaluator_links[0].dataset_evaluator.name, "match")
            self.assertEqual(session.scalar(select(func.count()).select_from(ExperimentPromptTask)), 1)

    def test_prompt_connection_and_provider_are_mutually_exclusive(self):
        with Session(self.engine) as session:
            dataset, version, *_, experiment, run = self.seed_experiment(session)
            session.add(ExperimentPromptTask(
                experiment=experiment, model_provider="OPENAI", model_name="example-model",
                template_type="CHAT", template_format="MUSTACHE", template={},
                custom_provider_id=1, connection={"type": "openai"},
            ))
            with self.assertRaises(IntegrityError):
                session.commit()


if __name__ == "__main__":
    unittest.main()
