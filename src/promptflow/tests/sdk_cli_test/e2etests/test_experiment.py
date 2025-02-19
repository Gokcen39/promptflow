import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from time import sleep

import pytest
from mock import mock
from ruamel.yaml import YAML

from promptflow import PFClient
from promptflow._sdk._constants import PF_TRACE_CONTEXT, ExperimentStatus, RunStatus, RunTypes
from promptflow._sdk._errors import ExperimentValueError, RunOperationError
from promptflow._sdk._load_functions import load_common
from promptflow._sdk.entities._experiment import CommandNode, Experiment, ExperimentTemplate, FlowNode

from ..recording_utilities import is_live

TEST_ROOT = Path(__file__).parent.parent.parent
EXP_ROOT = TEST_ROOT / "test_configs/experiments"
FLOW_ROOT = TEST_ROOT / "test_configs/flows"


yaml = YAML(typ="safe")


@pytest.mark.e2etest
@pytest.mark.usefixtures("setup_experiment_table")
class TestExperiment:
    def wait_for_experiment_terminated(self, client, experiment):
        while experiment.status in [ExperimentStatus.IN_PROGRESS, ExperimentStatus.QUEUING]:
            experiment = client._experiments.get(experiment.name)
            sleep(10)
        return experiment

    def test_experiment_from_template_with_script_node(self):
        template_path = EXP_ROOT / "basic-script-template" / "basic-script.exp.yaml"
        # Load template and create experiment
        template = load_common(ExperimentTemplate, source=template_path)
        experiment = Experiment.from_template(template)
        # Assert command node load correctly
        assert len(experiment.nodes) == 4
        expected = dict(yaml.load(open(template_path, "r", encoding="utf-8").read()))
        experiment_dict = experiment._to_dict()
        assert isinstance(experiment.nodes[0], CommandNode)
        assert isinstance(experiment.nodes[1], FlowNode)
        assert isinstance(experiment.nodes[2], FlowNode)
        assert isinstance(experiment.nodes[3], CommandNode)
        gen_data_snapshot_path = experiment._output_dir / "snapshots" / "gen_data"
        echo_snapshot_path = experiment._output_dir / "snapshots" / "echo"
        expected["nodes"][0]["code"] = gen_data_snapshot_path.absolute().as_posix()
        expected["nodes"][3]["code"] = echo_snapshot_path.absolute().as_posix()
        expected["nodes"][3]["environment_variables"] = {}
        assert experiment_dict["nodes"][0].items() == expected["nodes"][0].items()
        assert experiment_dict["nodes"][3].items() == expected["nodes"][3].items()
        # Assert snapshots
        assert gen_data_snapshot_path.exists()
        file_count = len(list(gen_data_snapshot_path.rglob("*")))
        assert file_count == 1
        assert (gen_data_snapshot_path / "generate_data.py").exists()
        # Assert no file exists in echo path
        assert echo_snapshot_path.exists()
        file_count = len(list(echo_snapshot_path.rglob("*")))
        assert file_count == 0

    def test_experiment_create_and_get(self):
        template_path = EXP_ROOT / "basic-no-script-template" / "basic.exp.yaml"
        # Load template and create experiment
        template = load_common(ExperimentTemplate, source=template_path)
        experiment = Experiment.from_template(template)
        client = PFClient()
        exp = client._experiments.create_or_update(experiment)
        assert len(client._experiments.list()) > 0
        exp_get = client._experiments.get(name=exp.name)
        assert exp_get._to_dict() == exp._to_dict()

    @pytest.mark.skipif(condition=not is_live(), reason="Injection cannot passed to detach process.")
    @pytest.mark.usefixtures("use_secrets_config_file", "recording_injection", "setup_local_connection")
    def test_experiment_start(self):
        template_path = EXP_ROOT / "basic-no-script-template" / "basic.exp.yaml"
        # Load template and create experiment
        template = load_common(ExperimentTemplate, source=template_path)
        experiment = Experiment.from_template(template)
        client = PFClient()
        exp = client._experiments.create_or_update(experiment)
        exp = client._experiments.start(exp.name)

        # Test the experiment in progress cannot be started.
        with pytest.raises(RunOperationError) as e:
            client._experiments.start(exp.name)
        assert f"Experiment {exp.name} is {exp.status}" in str(e.value)
        assert exp.status in [ExperimentStatus.IN_PROGRESS, ExperimentStatus.QUEUING]
        exp = self.wait_for_experiment_terminated(client, exp)
        # Assert main run
        assert len(exp.node_runs["main"]) > 0
        main_run = client.runs.get(name=exp.node_runs["main"][0]["name"])
        assert main_run.status == RunStatus.COMPLETED
        assert main_run.variant == "${summarize_text_content.variant_0}"
        assert main_run.display_name == "main"
        assert len(exp.node_runs["eval"]) > 0
        # Assert eval run and metrics
        eval_run = client.runs.get(name=exp.node_runs["eval"][0]["name"])
        assert eval_run.status == RunStatus.COMPLETED
        assert eval_run.display_name == "eval"
        metrics = client.runs.get_metrics(name=eval_run.name)
        assert "accuracy" in metrics

        # Test experiment restart
        exp = client._experiments.start(exp.name)
        exp = self.wait_for_experiment_terminated(client, exp)
        for name, runs in exp.node_runs.items():
            assert all([run["status"] == RunStatus.COMPLETED] for run in runs)

    @pytest.mark.skipif(condition=not is_live(), reason="Injection cannot passed to detach process.")
    @pytest.mark.usefixtures("use_secrets_config_file", "recording_injection", "setup_local_connection")
    def test_experiment_with_script_start(self):
        template_path = EXP_ROOT / "basic-script-template" / "basic-script.exp.yaml"
        # Load template and create experiment
        template = load_common(ExperimentTemplate, source=template_path)
        experiment = Experiment.from_template(template)
        client = PFClient()
        exp = client._experiments.create_or_update(experiment)
        exp = client._experiments.start(exp.name)
        exp = self.wait_for_experiment_terminated(client, exp)
        assert exp.status == ExperimentStatus.TERMINATED
        assert len(exp.node_runs) == 4
        for key, val in exp.node_runs.items():
            assert val[0]["status"] == RunStatus.COMPLETED, f"Node {key} run failed"
        run = client.runs.get(name=exp.node_runs["echo"][0]["name"])
        assert run.type == RunTypes.COMMAND

    @pytest.mark.skipif(condition=not is_live(), reason="Injection cannot passed to detach process.")
    @pytest.mark.usefixtures("use_secrets_config_file", "recording_injection", "setup_local_connection")
    def test_experiment_start_from_nodes(self):
        template_path = EXP_ROOT / "basic-script-template" / "basic-script.exp.yaml"
        # Load template and create experiment
        template = load_common(ExperimentTemplate, source=template_path)
        experiment = Experiment.from_template(template)
        client = PFClient()
        exp = client._experiments.create_or_update(experiment)
        exp = client._experiments.start(exp.name)
        exp = self.wait_for_experiment_terminated(client, exp)

        # Test start experiment from nodes
        exp = client._experiments.start(exp.name, from_nodes=["main"])
        exp = self.wait_for_experiment_terminated(client, exp)

        assert exp.status == ExperimentStatus.TERMINATED
        assert len(exp.node_runs) == 4
        for key, val in exp.node_runs.items():
            assert all([item["status"] == RunStatus.COMPLETED for item in val]), f"Node {key} run failed"
        assert len(exp.node_runs["main"]) == 2
        assert len(exp.node_runs["eval"]) == 2
        assert len(exp.node_runs["echo"]) == 2

        # Test run nodes in experiment
        exp = client._experiments.start(exp.name, nodes=["main"])
        exp = self.wait_for_experiment_terminated(client, exp)

        assert exp.status == ExperimentStatus.TERMINATED
        assert len(exp.node_runs) == 4
        for key, val in exp.node_runs.items():
            assert all([item["status"] == RunStatus.COMPLETED for item in val]), f"Node {key} run failed"
        assert len(exp.node_runs["main"]) == 3
        assert len(exp.node_runs["echo"]) == 2

    @pytest.mark.skipif(condition=not is_live(), reason="Injection cannot passed to detach process.")
    def test_cancel_experiment(self):
        template_path = EXP_ROOT / "command-node-exp-template" / "basic-command.exp.yaml"
        # Load template and create experiment
        template = load_common(ExperimentTemplate, source=template_path)
        experiment = Experiment.from_template(template)
        client = PFClient()
        exp = client._experiments.create_or_update(experiment)
        exp = client._experiments.start(exp.name)
        assert exp.status in [ExperimentStatus.IN_PROGRESS, ExperimentStatus.QUEUING]
        sleep(10)
        client._experiments.stop(exp.name)
        exp = client._experiments.get(exp.name)
        assert exp.status == ExperimentStatus.TERMINATED

    @pytest.mark.usefixtures("use_secrets_config_file", "recording_injection", "setup_local_connection")
    def test_flow_test_with_experiment(self, monkeypatch):
        # set queue size to 1 to make collection faster
        monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "1")
        monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "1")

        def _assert_result(result):
            assert "main" in result, "Node main not in result"
            assert "category" in result["main"], "Node main.category not in result"
            assert "evidence" in result["main"], "Node main.evidence not in result"
            assert "eval" in result, "Node eval not in result"
            assert "grade" in result["eval"], "Node eval.grade not in result"

        with mock.patch("promptflow._sdk._configuration.Configuration.is_internal_features_enabled") as mock_func:
            mock_func.return_value = True

            template_path = EXP_ROOT / "basic-no-script-template" / "basic.exp.yaml"
            target_flow_path = FLOW_ROOT / "web_classification" / "flow.dag.yaml"
            client = PFClient()
            session = str(uuid.uuid4())
            # Test with inputs
            result = client.flows.test(
                target_flow_path,
                experiment=template_path,
                inputs={"url": "https://www.youtube.com/watch?v=kYqRtjDBci8", "answer": "Channel"},
                session=session,
            )
            _assert_result(result)
            # Assert line run id is set by executor when running test
            assert PF_TRACE_CONTEXT in os.environ
            attributes = json.loads(os.environ[PF_TRACE_CONTEXT]).get("attributes")
            assert attributes.get("experiment") == template_path.resolve().absolute().as_posix()
            assert attributes.get("referenced.line_run_id", "").startswith("main")
            expected_output_path = (
                Path(tempfile.gettempdir()) / ".promptflow/sessions/default" / "basic-no-script-template"
            )
            assert expected_output_path.resolve().exists()
            # Assert eval metric exists
            assert (expected_output_path / "eval" / "flow.metrics.json").exists()
            # Assert session exists
            # TODO: Task 2942400, avoid sleep/if and assert traces
            time.sleep(10)  # TODO fix this
            line_runs = client._traces.list_line_runs(session_id=session)
            if len(line_runs) > 0:
                assert len(line_runs) == 1
                line_run = line_runs[0]
                assert "main_attempt" in line_run.line_run_id
                assert len(line_run.evaluations) > 0, "line run evaluation not exists!"
                assert "eval_classification_accuracy" in line_run.evaluations
            # Test with default data and custom path
            expected_output_path = Path(tempfile.gettempdir()) / ".promptflow/my_custom"
            result = client.flows.test(target_flow_path, experiment=template_path, output_path=expected_output_path)
            _assert_result(result)
            assert expected_output_path.resolve().exists()
            # Assert eval metric exists
            assert (expected_output_path / "eval" / "flow.metrics.json").exists()

        monkeypatch.delenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE")
        monkeypatch.delenv("OTEL_BSP_SCHEDULE_DELAY")

    def test_flow_not_in_experiment(self):
        template_path = EXP_ROOT / "basic-no-script-template" / "basic.exp.yaml"
        target_flow_path = FLOW_ROOT / "chat_flow" / "flow.dag.yaml"
        client = PFClient()
        with mock.patch("promptflow._sdk._configuration.Configuration.is_internal_features_enabled") as mock_func:
            mock_func.return_value = True
            with pytest.raises(ExperimentValueError) as error:
                client.flows.test(
                    target_flow_path,
                    experiment=template_path,
                )
            assert "not found in experiment" in str(error.value)
