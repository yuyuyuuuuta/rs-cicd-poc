import copy
import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest import mock

import terraform_apply as workflow
from terraform_apply import extract_apply_log, fenced_log, select_targets, validate_pr


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
        messages = "=== TERRAFORM_APPLY_BEGIN:root ===\nApply complete!\n=== TERRAFORM_APPLY_END:root ==="
        with mock.patch.object(workflow, "read_log_events", return_value=messages), mock.patch.object(workflow, "summary"):
            workflow.report_execution(aws, "pipeline", "execution", "root")
        self.assertEqual(aws.call.call_args_list[1].kwargs["next_token"], "page2")
        self.assertEqual(aws.call.call_args_list[2].kwargs["ids"], ["latest"])


class ApplyAndMergeTest(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "PR_NUMBER": "7", "EXPECTED_HEAD_SHA": "a" * 40, "EXPECTED_BASE_SHA": "b" * 40,
            "TARGET_ROOTS": '["terraform/env/poc/ci-test"]', "GITHUB_RUN_ID": "100", "GITHUB_RUN_ATTEMPT": "1",
            "TF_PIPELINE_NAMES": '{"terraform/env/poc/ci-test":"example-pipeline"}',
        }
        self.environment_patch = mock.patch.dict(os.environ, self.environment)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.patches = {}
        for name in ("GitHub", "Aws", "snapshot", "summary", "output", "report_execution", "wait_pipeline"):
            patcher = mock.patch.object(workflow, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        self.patches["Aws"].return_value.call.return_value = {"pipelineExecutionId": "execution"}
        self.patches["wait_pipeline"].return_value = {
            "status": "Succeeded", "artifactRevisions": [{"name": "SourceOutput", "revisionId": "a" * 40}],
        }

    def test_success_uses_exact_sha_and_publishes_success_only_after_logs(self):
        workflow.apply()
        arguments = self.patches["Aws"].return_value.call.call_args.kwargs
        self.assertEqual(arguments["source_revisions"][0]["revisionValue"], "a" * 40)
        self.assertEqual(arguments["variables"], [{"name": "PrCommitSha", "value": "a" * 40}])
        self.patches["report_execution"].assert_called_once()
        self.patches["output"].assert_any_call("applied_head", "a" * 40)
        self.patches["output"].assert_any_call("applied_base", "b" * 40)

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
        with self.assertRaises(RuntimeError):
            workflow.apply()
        self.patches["output"].assert_not_called()

    def test_changed_pr_blocks_aws_start(self):
        self.patches["snapshot"].side_effect = ValueError("PR head changed")
        with self.assertRaises(ValueError):
            workflow.apply()
        self.patches["Aws"].return_value.call.assert_not_called()

    def test_merge_uses_github_atomic_head_sha_guard(self):
        github = self.patches["GitHub"].return_value
        github.request.return_value = {"merged": True, "sha": "d" * 40}
        workflow.merge()
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
            contents = (root / path).read_text(encoding="utf-8")
            with self.subTest(path=path):
                for pattern in patterns:
                    self.assertIsNone(re.search(pattern, contents), f"Private identifier or credential pattern in {path}")


if __name__ == "__main__":
    unittest.main()