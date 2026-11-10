import copy
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

import terraform_apply as workflow
from terraform_apply import extract_apply_log, fenced_log, select_targets, validate_pr
import yaml


class WorkflowGuardsTest(unittest.TestCase):
    def setUp(self):
        self.repository = "example-owner/example-repository"
        self.pr = {
            "state": "open",
            "draft": False,
            "mergeable": True,
            "head": {"sha": "a" * 40, "repo": {"full_name": self.repository}},
            "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": self.repository}},
        }

    def test_open_same_repository_pr(self):
        self.assertEqual(validate_pr(self.pr, self.repository), ("a" * 40, "b" * 40))

    def test_reject_unsafe_pr_metadata(self):
        for field, value in (("state", "closed"), ("draft", True), ("mergeable", False), ("mergeable", None)):
            with self.subTest(field=field, value=value):
                candidate = copy.deepcopy(self.pr)
                candidate[field] = value
                with self.assertRaises(ValueError):
                    validate_pr(candidate, self.repository)

    def test_reject_fork_or_deleted_head_repository(self):
        for repository in ({"full_name": "someone/fork"}, None):
            candidate = copy.deepcopy(self.pr)
            candidate["head"]["repo"] = repository
            with self.assertRaisesRegex(ValueError, "Fork"):
                validate_pr(candidate, self.repository)

    def test_reject_wrong_base_branch(self):
        self.pr["base"]["ref"] = "other"
        with self.assertRaises(ValueError):
            validate_pr(self.pr, self.repository)

    def test_reject_new_head_or_base(self):
        with self.assertRaisesRegex(ValueError, "head changed"):
            validate_pr(self.pr, self.repository, expected_head="c" * 40)
        with self.assertRaisesRegex(ValueError, "base changed"):
            validate_pr(self.pr, self.repository, expected_base="c" * 40)

    def test_empty_expected_sha_cannot_bypass_comparison(self):
        with self.assertRaises(ValueError):
            validate_pr(self.pr, self.repository, expected_head="")
        with self.assertRaises(ValueError):
            validate_pr(self.pr, self.repository, expected_base="")

    def test_reject_invalid_sha(self):
        self.pr["head"]["sha"] = "$(untrusted)"
        with self.assertRaises(ValueError):
            validate_pr(self.pr, self.repository)

    def test_select_only_registered_roots(self):
        self.assertEqual(
            select_targets(["terraform/env/poc/ci-test/s3.tf", "README.md"]),
            ["terraform/env/poc/ci-test"],
        )

    def test_shared_module_selects_only_registered_consumers(self):
        with mock.patch.object(workflow, "DEPENDENCIES", {"terraform/env/poc/ci-test": {"terraform/modules/storage"}}):
            self.assertEqual(select_targets(["terraform/modules/storage/main.tf"]), ["terraform/env/poc/ci-test"])
            with self.assertRaises(ValueError):
                select_targets(["terraform/modules/unknown/main.tf"])

    def test_saved_plan_is_bound_to_approval_and_expires(self):
        expected = dict(repository=self.repository, pr="9", head="a" * 40, base="b" * 40,
                        root="terraform/env/poc/ci-test", run_id="123", attempt="1")
        manifest = dict(expected, created=100, expires=86500, sha256="c" * 64, plan_version="version-1")
        workflow.validate_manifest(manifest, expected, now=101)
        for field in expected:
            with self.subTest(field=field), self.assertRaises(ValueError):
                workflow.validate_manifest(dict(manifest, **{field: "wrong"}), expected, now=101)
        for changes in ({"expires": 101}, {"expires": 86501}, {"created": 102}, {"sha256": "bad"}, {"plan_version": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                workflow.validate_manifest(dict(manifest, **changes), expected, now=101)

    def test_find_open_pr_by_branch_uses_same_repository_match(self):
        github = mock.Mock()
        github.repository = self.repository
        github.request.return_value = [
            {
                "number": 9,
                "head": {"ref": "feature/apply", "repo": {"full_name": self.repository}},
            }
        ]
        pr = workflow.find_open_pr_by_branch(github, "feature/apply")
        self.assertEqual(pr["number"], 9)
        self.assertIn("head=example-owner%3Afeature%2Fapply", github.request.call_args.args[0])

    def test_find_open_pr_rejects_ambiguous_or_fork_branches(self):
        github = mock.Mock()
        github.repository = self.repository
        valid = {"number": 9, "head": {"ref": "feature", "repo": {"full_name": self.repository}}}
        fork = {"number": 10, "head": {"ref": "feature", "repo": {"full_name": "other/fork"}}}
        for result in ([], [valid, valid], [fork]):
            with self.subTest(result=result):
                github.request.return_value = result
                with self.assertRaises(ValueError):
                    workflow.find_open_pr_by_branch(github, "feature")

    def test_pr_branch_rejects_option_and_path_traversal(self):
        for branch in ("", "../main", "-x", "feature//branch", "feature/../main"):
            with self.subTest(branch=branch), mock.patch.dict(os.environ, {"PR_BRANCH": branch}):
                with self.assertRaises(ValueError):
                    workflow.pr_branch()

    def test_apply_requires_successful_plan_for_exact_base(self):
        github = mock.Mock()
        github.repository = self.repository
        status = {
            "statuses": [{
                "context": workflow.PLAN_STATUS_CONTEXT,
                "state": "success",
                "description": f"Plan passed for base {'b' * 40}",
                "created_at": "2026-11-10T00:00:00Z",
                "target_url": f"https://github.com/{self.repository}/actions/runs/10",
            }]
        }
        run = dict(id=10, run_attempt=1, conclusion="success", event="pull_request_target",
                   path=".github/workflows/terraform-plan.yml", head_sha="b" * 40,
                   repository={"full_name": self.repository})
        github.request.side_effect = lambda path: run if path.startswith("actions/") else status
        workflow.require_successful_plan(github, "a" * 40, "b" * 40)
        for field, invalid in (("conclusion", "failure"), ("path", ".github/workflows/untrusted.yml"), ("head_sha", "c" * 40)):
            original = run[field]
            run[field] = invalid
            with self.subTest(field=field), self.assertRaises(ValueError):
                workflow.require_successful_plan(github, "a" * 40, "b" * 40)
            run[field] = original
        with self.assertRaises(ValueError):
            workflow.require_successful_plan(github, "a" * 40, "c" * 40)
        status["statuses"][0]["state"] = "failure"
        with self.assertRaises(ValueError):
            workflow.require_successful_plan(github, "a" * 40, "b" * 40)

    def test_plan_and_apply_use_the_same_root_scoped_pipeline_map(self):
        value = '{"terraform/env/poc/ci-test":"shared-pipeline"}'
        with mock.patch.dict(os.environ, {"TF_PIPELINE_NAMES": value}):
            self.assertEqual(workflow.pipeline_names(), {"terraform/env/poc/ci-test": "shared-pipeline"})

    def test_plan_status_success_is_recorded_for_exact_commit(self):
        github = mock.Mock()
        github.request.return_value = {"context": workflow.PLAN_STATUS_CONTEXT}
        workflow.set_plan_status(github, "a" * 40, "success", "Plan passed for base " + "b" * 40, "https://example.test/run")
        github.request.assert_called_once_with(
            "statuses/" + "a" * 40,
            method="POST",
            data={
                "state": "success",
                "context": workflow.PLAN_STATUS_CONTEXT,
                "description": "Plan passed for base " + "b" * 40,
                "target_url": "https://example.test/run",
            },
        )

    def test_finalize_plan_status_fails_closed_when_any_stage_failed(self):
        environment = {
            "PLAN_HEAD_SHA": "a" * 40,
            "PLAN_BASE_SHA": "b" * 40,
            "PREFLIGHT_RESULT": "success",
            "STATIC_CHECK_RESULT": "failure",
            "PLAN_RESULT": "skipped",
        }
        with mock.patch.dict(os.environ, environment), mock.patch.object(workflow, "GitHub") as github_class:
            with mock.patch.object(workflow, "summary"):
                workflow.finalize_plan_status()
        github_class.return_value.request.assert_called_once()
        status_data = github_class.return_value.request.call_args.kwargs["data"]
        self.assertEqual(status_data["state"], "failure")

    def test_finalize_plan_status_success_is_base_bound(self):
        environment = {
            "PLAN_HEAD_SHA": "a" * 40,
            "PLAN_BASE_SHA": "b" * 40,
            "PREFLIGHT_RESULT": "success",
            "STATIC_CHECK_RESULT": "success",
            "PLAN_RESULT": "success",
            "PLAN_RUN_URL": "https://example.test/run",
        }
        with mock.patch.dict(os.environ, environment), mock.patch.object(workflow, "GitHub") as github_class:
            with mock.patch.object(workflow, "summary"):
                workflow.finalize_plan_status()
        data = github_class.return_value.request.call_args.kwargs["data"]
        self.assertEqual(data["state"], "success")
        self.assertEqual(data["description"], "Plan passed for base " + "b" * 40)


class WorkflowYamlTest(unittest.TestCase):
    def test_automatic_plan_and_label_selected_approved_apply(self):
        root = Path(__file__).resolve().parents[2]
        plan = yaml.load((root / ".github/workflows/terraform-plan.yml").read_text(), Loader=yaml.BaseLoader)
        apply = yaml.load((root / ".github/workflows/terraform-apply.yml").read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(plan["on"]), {"pull_request_target"})
        self.assertEqual(plan["on"]["pull_request_target"]["branches"], ["main"])
        self.assertNotIn("workflow_dispatch", apply["on"])
        self.assertIn("labeled", apply["on"]["pull_request_target"]["types"])
        self.assertIn("apply-request", apply["jobs"]["inspect"]["if"])
        self.assertEqual(apply["jobs"]["aws-apply"]["environment"], "terraform-apply")
        self.assertNotIn("environment", apply["jobs"]["aws-replan"])
        self.assertEqual(apply["jobs"]["aws-apply"]["needs"], ["inspect", "aws-replan"])
        self.assertIn("SAVED_PLANS", apply["jobs"]["aws-apply"]["steps"][-1]["env"])
        self.assertEqual(set(plan["jobs"]["inspect"]["permissions"].values()), {"read", "write"})
        self.assertEqual(plan["jobs"]["aws-plan"]["permissions"]["id-token"], "write")
        self.assertEqual(apply["jobs"]["merge"]["needs"], ["inspect", "aws-apply"])


class WorkflowConfigurationTest(unittest.TestCase):
    def test_plan_config_requires_plan_role_bucket_and_pipeline_map(self):
        environment = {
            "AWS_ACCOUNT_ID": "1" * 12,
            "AWS_REGION": "us-east-1",
            "TF_STATE_BUCKET": "state-bucket",
            "TF_TEST_BUCKET": "test-bucket",
            "TF_PLAN_ARTIFACT_BUCKET": "plan-artifact-bucket",
            "AWS_GITHUB_PLAN_ROLE_ARN": "arn:aws:iam::" + "1" * 12 + ":role/plan-role",
            "TF_PIPELINE_NAMES": '{"terraform/env/poc/ci-test":"shared-pipeline"}',
        }
        with mock.patch.dict(os.environ, environment):
            workflow.check_plan_config()
        environment.pop("AWS_GITHUB_PLAN_ROLE_ARN")
        with mock.patch.dict(os.environ, environment), self.assertRaises(ValueError):
            workflow.check_plan_config()

    def test_reject_control_plane_and_unknown_changes(self):
        for path in (
            ".github/workflows/terraform-apply.yml",
            "terraform/env/poc/infra-cicd-apply/iam.tf",
            "terraform/bootstrap/state-bucket/s3.tf",
            "admin/iam/policy.json",
            "terraform/env/poc/ci-test-other/s3.tf",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                select_targets(["terraform/env/poc/ci-test/s3.tf", path])

    def test_docs_only_pr_cannot_apply_or_merge(self):
        with self.assertRaises(ValueError):
            select_targets(["README.md"])


class ApplyLogTest(unittest.TestCase):
    def test_extract_and_normalize(self):
        text = (
            "install output\n=== TERRAFORM_APPLY_BEGIN:terraform/env/poc/ci-test ===\n"
            "\x1b[32mApply complete!\x1b[0m\n\n\nOutputs:\n"
            "=== TERRAFORM_APPLY_END:terraform/env/poc/ci-test ===\nTerraform apply exit code: 0"
        )
        self.assertEqual(extract_apply_log(text, "terraform/env/poc/ci-test"), "Apply complete!\n\nOutputs:")

    def test_missing_or_wrong_root_markers_fail(self):
        for text in ("", "=== TERRAFORM_APPLY_BEGIN:other ===\noutput"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                extract_apply_log(text, "terraform/env/poc/ci-test")

    def test_log_cannot_close_the_markdown_fence(self):
        self.assertTrue(fenced_log("```\nraw output").startswith("````text\n"))

    def test_large_logs_are_truncated(self):
        result = fenced_log("x" * 70000)
        self.assertIn("[log truncated]", result)
        self.assertLess(len(result), 61000)


class AwsExecutionTest(unittest.TestCase):
    def test_waits_until_pipeline_is_terminal(self):
        aws = mock.Mock()
        aws.call.side_effect = [
            {"pipelineExecution": {"pipelineExecutionId": "execution", "status": status}}
            for status in ("InProgress", "Stopping", "Succeeded")
        ]
        pause = mock.Mock()
        clock = mock.Mock(side_effect=[0, 1, 2, 3])
        execution = workflow.wait_pipeline(aws, "pipeline", "execution", pause=pause, clock=clock)
        self.assertEqual(execution["status"], "Succeeded")
        self.assertEqual(pause.call_count, 2)

    def test_wait_timeout_fails_without_merging_or_stopping_aws(self):
        aws = mock.Mock()
        with self.assertRaises(TimeoutError):
            workflow.wait_pipeline(aws, "pipeline", "execution", clock=mock.Mock(side_effect=[0, 2401]))
        aws.call.assert_not_called()

    def test_wrong_execution_id_fails(self):
        aws = mock.Mock()
        aws.call.return_value = {"pipelineExecution": {"pipelineExecutionId": "other", "status": "Succeeded"}}
        with self.assertRaises(ValueError):
            workflow.wait_pipeline(aws, "pipeline", "execution")

    def test_logs_continue_through_an_empty_page_with_a_new_token(self):
        aws = mock.Mock()
        aws.call.side_effect = [
            {"events": [{"message": "first"}], "nextForwardToken": "token1"},
            {"events": [], "nextForwardToken": "token2"},
            {"events": [{"message": "second"}], "nextForwardToken": "token3"},
            {"events": [], "nextForwardToken": "token3"},
        ]
        self.assertEqual(workflow.read_log_events(aws, "group", "stream"), "first\nsecond")
        self.assertEqual(aws.call.call_args_list[2].kwargs["next_token"], "token2")

    def test_log_size_limit_fails_closed(self):
        aws = mock.Mock()
        aws.call.return_value = {"events": [{"message": "x" * 2000001}]}
        with self.assertRaisesRegex(ValueError, "size limit"):
            workflow.read_log_events(aws, "group", "stream")

    def test_action_pagination_selects_latest_build(self):
        aws = mock.Mock()
        actions = [
            {
                "actionName": "TerraformApply", "stageName": "Apply", "startTime": timestamp,
                "output": {"executionResult": {"externalExecutionId": build}},
            }
            for timestamp, build in (("2026-01-01", "older"), ("2026-01-02", "latest"))
        ]
        aws.call.side_effect = [
            {"actionExecutionDetails": [actions[0]], "nextToken": "page2"},
            {"actionExecutionDetails": [actions[1]]},
            {"builds": [{"buildStatus": "SUCCEEDED", "logs": {"groupName": "group", "streamName": "stream"}}]},
        ]
        messages = 'TF_RESULT_JSON:{"phase":"apply","root":"root","status":"SUCCEEDED","counts":{"add":1},"changes":[]}'
        with mock.patch.object(workflow, "read_log_events", return_value=messages), mock.patch.object(workflow, "summary"):
            workflow.report_execution(aws, "pipeline", "execution", "root")
        self.assertEqual(aws.call.call_args_list[1].kwargs["next_token"], "page2")
        self.assertEqual(aws.call.call_args_list[2].kwargs["ids"], ["latest"])


class ApplyAndMergeTest(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "PR_NUMBER": "7", "EXPECTED_HEAD_SHA": "a" * 40, "EXPECTED_BASE_SHA": "b" * 40,
            "PR_BRANCH": "feature/apply",
            "TARGET_ROOTS": '["terraform/env/poc/ci-test"]', "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
            "TF_PIPELINE_NAMES": '{"terraform/env/poc/ci-test":"example-pipeline"}',
            "GITHUB_REPOSITORY": "owner/repo",
            "SAVED_PLANS": '{"terraform/env/poc/ci-test":{"manifest_version":"v1","sha256":"digest"}}',
        }
        self.environment_patch = mock.patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.patches = {}
        for name in (
            "GitHub", "Aws", "snapshot", "summary", "output", "report_execution", "wait_pipeline",
            "require_successful_plan", "find_open_pr_by_branch", "read_manifest", "authorize_plan", "label_event",
        ):
            patcher = mock.patch.object(workflow, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.patches["Aws"].return_value.call.return_value = {"pipelineExecutionId": "execution"}
        self.patches["require_successful_plan"].return_value = ("90", "1")
        self.patches["snapshot"].return_value = ({"mergeable_state": "clean"}, "a" * 40, "b" * 40)
        self.patches["report_execution"].return_value = dict(repository="owner/repo", pr="7", head="a" * 40, base="b" * 40,
                                 root="terraform/env/poc/ci-test", run_id="100", attempt="1")
        self.patches["find_open_pr_by_branch"].return_value = {
            "number": 7, "head": {"ref": "feature/apply"},
        }
        self.patches["wait_pipeline"].return_value = {
            "status": "Succeeded", "artifactRevisions": [{"name": "SourceOutput", "revisionId": "a" * 40}],
        }

    def test_success_uses_exact_sha_and_publishes_success_only_after_logs(self):
        workflow.apply()
        arguments = self.patches["Aws"].return_value.call.call_args.kwargs
        self.assertEqual(arguments["source_revisions"][0]["revisionValue"], "a" * 40)
        variables = {item["name"]: item["value"] for item in arguments["variables"]}
        self.assertEqual(variables["PrCommitSha"], "a" * 40)
        self.assertEqual(variables["ExecutionMode"], "apply")
        self.assertEqual(variables["ManifestVersion"], "v1")
        self.patches["authorize_plan"].assert_called_once()
        self.patches["report_execution"].assert_called_once()
        self.patches["output"].assert_any_call("applied_head", "a" * 40)
        self.patches["output"].assert_any_call("applied_base", "b" * 40)
        self.assertEqual(self.patches["require_successful_plan"].call_count, 1)

    def test_failed_pipeline_still_collects_logs_and_has_no_success_output(self):
        self.patches["wait_pipeline"].return_value = {"status": "Failed"}
        with self.assertRaises(ValueError):
            workflow.apply()
        self.patches["report_execution"].assert_called_once()
        self.patches["output"].assert_not_called()

    def test_wrong_artifact_sha_blocks_success(self):
        self.patches["wait_pipeline"].return_value["artifactRevisions"][0]["revisionId"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "revision"):
            workflow.apply()
        self.patches["output"].assert_not_called()

    def test_log_retrieval_failure_blocks_success(self):
        self.patches["report_execution"].side_effect = RuntimeError("AccessDenied")
        with self.assertRaises(ValueError):
            workflow.apply()
        self.patches["output"].assert_not_called()

    def test_changed_pr_blocks_aws_start(self):
        self.patches["snapshot"].side_effect = ValueError("PR head changed")
        with self.assertRaises(ValueError):
            workflow.apply()
        self.patches["Aws"].return_value.call.assert_not_called()

    def test_branch_re_resolving_to_another_pr_blocks_aws_start(self):
        self.patches["find_open_pr_by_branch"].return_value["number"] = 8
        with self.assertRaisesRegex(ValueError, "PR resolved"):
            workflow.apply()
        self.patches["Aws"].return_value.call.assert_not_called()

    def test_merge_uses_github_atomic_head_sha_guard(self):
        github = self.patches["GitHub"].return_value
        github.request.return_value = {"merged": True, "sha": "d" * 40}
        workflow.merge()
        self.patches["require_successful_plan"].assert_called_once_with(github, "a" * 40, "b" * 40)
        github.request.assert_called_once_with(
            "pulls/7/merge", method="PUT", data={"sha": "a" * 40, "merge_method": "merge"},
        )

    def test_merge_rejection_is_not_reported_as_success(self):
        self.patches["GitHub"].return_value.request.return_value = {"merged": False}
        with self.assertRaises(ValueError):
            workflow.merge()
        self.patches["summary"].assert_not_called()

    def test_changed_pr_blocks_merge_request(self):
        self.patches["snapshot"].side_effect = ValueError("PR head changed")
        with self.assertRaises(ValueError):
            workflow.merge()
        self.patches["GitHub"].return_value.request.assert_not_called()

    def test_branch_reuse_cannot_merge_a_different_pr(self):
        self.patches["find_open_pr_by_branch"].return_value["number"] = 8
        with self.assertRaisesRegex(ValueError, "different PR"):
            workflow.merge()
        self.patches["GitHub"].return_value.request.assert_not_called()

    def test_incomplete_plan_set_blocks_apply(self):
        with mock.patch.dict(os.environ, {"SAVED_PLANS": "{}"}), self.assertRaises(ValueError):
            workflow.apply()
        self.patches["authorize_plan"].assert_not_called()

    def test_blocked_checks_prevent_merge(self):
        self.patches["snapshot"].return_value = ({"mergeable_state": "blocked"}, "a" * 40, "b" * 40)
        with self.assertRaises(ValueError):
            workflow.merge()
        self.patches["GitHub"].return_value.request.assert_not_called()

    def test_one_module_failure_still_collects_other_module_results(self):
        roots = ["terraform/env/poc/ci-test", "terraform/env/poc/other"]
        environment = dict(TARGET_ROOTS=json.dumps(roots), TF_PIPELINE_NAMES=json.dumps(dict(zip(roots, ("first", "second")))),
                           SAVED_PLANS=json.dumps({root: {"manifest_version": "v1", "sha256": "digest"} for root in roots}))
        self.patches["wait_pipeline"].side_effect = [{"status": "Failed"}, self.patches["wait_pipeline"].return_value]
        self.patches["report_execution"].side_effect = [dict(self.patches["report_execution"].return_value, root=root) for root in roots]
        with mock.patch.object(workflow, "TARGETS", set(roots)), mock.patch.dict(os.environ, environment):
            with self.assertRaisesRegex(ValueError, "modules failed"):
                workflow.apply()
        self.assertEqual(self.patches["report_execution"].call_count, 2)
        self.patches["output"].assert_not_called()


class WorkflowPreflightTest(unittest.TestCase):
    def setUp(self):
        self.repository = "example-owner/example-repository"
        self.pr = {
            "number": 12,
            "state": "open",
            "draft": False,
            "mergeable": True,
            "changed_files": 1,
            "head": {"sha": "a" * 40, "ref": "feature/ci-test", "repo": {"full_name": self.repository}},
            "base": {"sha": "b" * 40, "ref": "main", "repo": {"full_name": self.repository}},
        }
        self.patches = {}
        for name in ("GitHub", "output", "summary", "select_targets", "set_plan_status", "require_successful_plan", "find_open_pr_by_branch"):
            patcher = mock.patch.object(workflow, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        github = self.patches["GitHub"].return_value
        github.repository = self.repository
        github.request.return_value = {"object": {"sha": "b" * 40}}
        github.files.return_value = ["terraform/env/poc/ci-test/s3.tf"]
        self.patches["select_targets"].return_value = ["terraform/env/poc/ci-test"]
        self.patches["find_open_pr_by_branch"].return_value = self.pr

    def test_automatic_plan_marks_event_head_pending(self):
        environment = {
            "GITHUB_EVENT_NAME": "pull_request_target",
            "PR_NUMBER": "12",
            "EVENT_PR_HEAD_SHA": "a" * 40,
            "EVENT_PR_BASE_SHA": "b" * 40,
        }
        self.patches["GitHub"].return_value.request.side_effect = [self.pr, {"object": {"sha": "b" * 40}}]
        with mock.patch.dict(os.environ, environment):
            workflow.prepare_plan()
        self.patches["set_plan_status"].assert_called_once_with(
            self.patches["GitHub"].return_value, "a" * 40, "pending", "Plan is running for base " + "b" * 40,
        )
        self.patches["output"].assert_any_call("pr_branch", "feature/ci-test")

    def test_label_apply_uses_event_pr_and_requires_plan(self):
        self.pr["labels"] = [{"name": "apply-request"}]
        event = {"pull_request": self.pr}
        github = self.patches["GitHub"].return_value
        github.request.side_effect = [self.pr, {"object": {"sha": "b" * 40}}]
        with mock.patch.object(workflow, "label_event", return_value=event):
            workflow.prepare_apply()
        self.patches["find_open_pr_by_branch"].assert_not_called()
        self.patches["require_successful_plan"].assert_called_once_with(
            self.patches["GitHub"].return_value, "a" * 40, "b" * 40,
        )

    def test_apply_label_requires_authorized_sender(self):
        event = dict(action="labeled", label={"name": "apply-request"}, sender={"login": "author"})
        environment = dict(GITHUB_EVENT_NAME="pull_request_target", GITHUB_REF="refs/heads/main", GITHUB_EVENT_PATH="event.json")
        github = self.patches["GitHub"].return_value
        github.request.return_value = {"permission": "read"}
        with mock.patch.dict(os.environ, environment), mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(event))):
            with self.assertRaisesRegex(ValueError, "write permission"):
                workflow.label_event(github)
            github.request.return_value = {"permission": "write"}
            self.assertEqual(workflow.label_event(github), event)
            with mock.patch.dict(os.environ, {"GITHUB_REF": "refs/heads/other"}), self.assertRaises(ValueError):
                workflow.label_event(github)


class IdentifierPrivacyTest(unittest.TestCase):
    def test_account_and_arn_redacted_without_secrets(self):
        account = "1" * 12
        text = f"owner={account} arn:aws:iam::{account}:role/example-role"
        result = workflow.redact_identifiers(text)
        self.assertNotIn(account, result)
        self.assertNotIn("example-role", result)

    def test_known_bucket_and_pipeline_names_redacted(self):
        environment = {
            "TF_STATE_BUCKET": "private-state-bucket",
            "TF_TEST_BUCKET": "private-test-bucket",
            "TF_PIPELINE_NAMES": '{"terraform/env/poc/ci-test":"private-pipeline"}',
        }
        with mock.patch.dict(os.environ, environment):
            result = workflow.fenced_log("private-state-bucket private-test-bucket private-pipeline")
        self.assertNotIn("private-state-bucket", result)
        self.assertNotIn("private-test-bucket", result)
        self.assertNotIn("private-pipeline", result)

    def test_request_identifiers_are_redacted(self):
        identifier = "abcdefab-cdef-abcd-efab-cdefabcdefab"
        self.assertNotIn(identifier, workflow.redact_identifiers(f"RequestID: {identifier}"))

    def test_pipeline_mapping_is_required_and_fixed(self):
        for value in ("{}", "[]", '{"unregistered":"pipeline"}', '{"terraform/env/poc/ci-test":""}'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TF_PIPELINE_NAMES": value}):
                with self.assertRaises(ValueError):
                    workflow.pipeline_names()

    def test_missing_secrets_fail_before_aws(self):
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            workflow.check_config()

    def test_secret_values_never_appear_in_configuration_error(self):
        environment = {
            "AWS_ACCOUNT_ID": "1" * 12,
            "AWS_REGION": "region-invalid",
            "AWS_GITHUB_ACTIONS_ROLE_ARN": "private-role",
            "TF_STATE_BUCKET": "private-state",
            "TF_TEST_BUCKET": "private-test",
            "TF_PIPELINE_NAMES": '{"terraform/env/poc/ci-test":"private-pipeline"}',
        }
        with mock.patch.dict(os.environ, environment):
            with self.assertRaises(ValueError) as error:
                workflow.check_config()
            self.assertNotIn("private-role", str(error.exception))


class TrustedRunnerTest(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2]
        path = root / "terraform/env/poc/infra-cicd-apply/buildspec/terraform_runner.py"
        spec = importlib.util.spec_from_file_location("trusted_runner", path)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_embedded_buildspec_python_and_shell_are_valid(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / "terraform/env/poc/infra-cicd-apply/buildspec/terraform_runner.py").read_text()
        ast.parse(source)
        for directory, name in (("infra-cicd-plan", "terraform-plan"), ("infra-cicd-apply", "terraform-apply")):
            with self.subTest(directory=directory):
                template = (root / f"terraform/env/poc/{directory}/buildspec/{name}.yml").read_text()
                rendered = template.replace("__TRUSTED_RUNNER__", source.replace("\n", "\n" + " " * 8))
                buildspec = yaml.safe_load(rendered)
                for phase in buildspec["phases"].values():
                    for command in phase["commands"]:
                        subprocess.run(["bash", "-n"], input=command, text=True, check=True, capture_output=True)
                command = buildspec["phases"]["build"]["commands"][0]
                embedded = command.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
                ast.parse(embedded)

    def test_summary_never_contains_attribute_or_output_values(self):
        result = self.runner.safe_changes({"resource_changes": [{
            "address": "example.resource", "mode": "managed",
            "change": {"actions": ["delete", "create"], "before": {"password": "old", "name": "private-old"},
                       "after": {"password": "secret", "name": "private-new"}, "after_sensitive": {"password": True}},
        }], "output_changes": {"password": {"after": "secret"}}})
        self.assertEqual(result["counts"], {"add": 1, "change": 0, "destroy": 1})
        self.assertEqual(result["changes"][0]["attributes"], ["name"])
        for private in ("secret", "password", "private-old", "private-new"):
            self.assertNotIn(private, str(result))

    def test_s3_download_uses_a_positional_output_file(self):
        with mock.patch.object(self.runner.subprocess, "run", return_value=mock.Mock(stdout="{}")) as execute:
            self.runner.aws("get-object", bucket="bucket", key="key", outfile="download.json")
        self.assertEqual(execute.call_args.args[0][-1], "download.json")
        self.assertNotIn("--outfile", execute.call_args.args[0])

    def test_plan_uploads_a_versioned_manifest_without_applying(self):
        with tempfile.TemporaryDirectory() as source:
            (Path(source) / "terraform/env/poc/ci-test").mkdir(parents=True)
            environment = dict(GITHUB_REPOSITORY_NAME="owner/repo", PR_NUMBER="1", EXPECTED_COMMIT_SHA="a" * 40,
                               SOURCE_COMMIT_SHA="a" * 40, BASE_COMMIT_SHA="b" * 40, TARGET_STATE_DIR="terraform/env/poc/ci-test",
                               PLAN_RUN_ID="10", PLAN_ATTEMPT="1", EXECUTION_MODE="plan", BUILD_MODE="plan",
                               CODEBUILD_SRC_DIR=source, PLAN_BUCKET="bucket", TF_VERSION="1.16.6", STATE_BUCKET="state",
                               STATE_KEY="state/key", AWS_DEFAULT_REGION="us-east-1", PIPELINE_EXECUTION_ID="execution")

            def terraform(command, **kwargs):
                for argument in command:
                    if argument.startswith("-out="):
                        Path(argument[5:]).write_bytes(b"saved-plan")
                return mock.Mock(returncode=0, stdout="{}" if "show" in command else "", stderr="")

            with mock.patch.dict(os.environ, environment), mock.patch.object(self.runner.subprocess, "run", side_effect=terraform) as execute:
                with mock.patch.object(self.runner, "aws", return_value={"VersionId": "v1"}) as aws, mock.patch("builtins.print") as printed, mock.patch.object(self.runner, "fresh_pr"):
                    self.runner.run()
            self.assertFalse(any("apply" in call.args[0] for call in execute.call_args_list))
            manifest = [call for call in aws.call_args_list if call.kwargs.get("key", "").endswith("/manifest.json")][0]
            self.assertEqual(manifest.kwargs["if_none_match"], "*")
            self.assertIn('"status":"SUCCEEDED"', printed.call_args.args[0])

    def test_apply_uses_only_saved_plan_and_rejects_tampering(self):
        for defect in (None, "approval", "digest", "expired", "duplicate", "updated-pr"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as source:
                (Path(source) / "terraform/env/poc/ci-test").mkdir(parents=True)
                binding = dict(repository="owner/repo", pr="1", head="a" * 40, base="b" * 40,
                               root="terraform/env/poc/ci-test", run_id="10", attempt="1")
                manifest = dict(binding, created=100, expires=300, terraform_version="1.16.6", plan_version="pv",
                                sha256=workflow.hashlib.sha256(b"plan").hexdigest(), changes=[])
                approval = dict(binding=binding, manifest_version="mv", sha256=manifest["sha256"])
                if defect == "approval":
                    approval["manifest_version"] = "other"
                if defect == "expired":
                    manifest["expires"] = 199
                environment = dict(EXECUTION_MODE="apply", BUILD_MODE="apply", CODEBUILD_SRC_DIR=source,
                                   PLAN_BUCKET="bucket", MANIFEST_VERSION="mv", TF_VERSION="1.16.6", STATE_BUCKET="state",
                                   STATE_KEY="state/key", AWS_DEFAULT_REGION="us-east-1", PIPELINE_EXECUTION_ID="execution")

                def download(operation, **arguments):
                    if operation == "get-object":
                        Path(arguments["outfile"]).write_bytes(b"tampered" if defect == "digest" else b"plan")
                    return {"VersionId": "version"}

                with mock.patch.dict(os.environ, environment), mock.patch.object(self.runner, "binding", return_value=binding), mock.patch.object(self.runner.time, "time", return_value=200):
                    with mock.patch.object(self.runner, "get_json", side_effect=[(manifest, "mv"), (approval, "av")]), mock.patch.object(self.runner, "aws", side_effect=download):
                        with mock.patch.object(self.runner, "put_json", side_effect=ValueError("duplicate") if defect == "duplicate" else None), mock.patch.object(self.runner, "fresh_pr", side_effect=ValueError("changed") if defect == "updated-pr" else None):
                            with mock.patch.object(self.runner.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="Apply complete! Resources: 1 added, 0 changed, 0 destroyed.", stderr="")) as execute, mock.patch("builtins.print"):
                                if defect:
                                    with self.assertRaises(ValueError):
                                        self.runner.run()
                                    self.assertFalse(any("apply" in call.args[0] for call in execute.call_args_list))
                                else:
                                    self.runner.run()
                                    self.assertTrue(any("apply" in call.args[0] for call in execute.call_args_list))
                                    self.assertFalse(any("plan" in call.args[0] for call in execute.call_args_list))


class PublicFilePrivacyTest(unittest.TestCase):
    def test_publishable_files_have_no_literal_account_ids_or_credentials(self):
        root = Path(__file__).resolve().parents[2]
        paths = subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root,
        ).decode().split("\0")
        patterns = [
            r"(?<![A-Za-z0-9])\d{12}(?![A-Za-z0-9])",
            r"(?:AKIA|ASIA)[A-Z0-9]{16}",
            r"github_pat_[A-Za-z0-9_]{20,}",
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----",
        ]
        for path in filter(None, paths):
            file_path = root / path
            if not file_path.is_file():
                continue
            contents = file_path.read_text(encoding="utf-8")
            with self.subTest(path=path):
                for pattern in patterns:
                    self.assertIsNone(re.search(pattern, contents), f"Private identifier or credential pattern in {path}")


if __name__ == "__main__":
    unittest.main()