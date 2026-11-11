import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    specification = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


runner = load_module("terraform_runner", "terraform/env/poc/infra-cicd-apply/buildspec/terraform_runner.py")
apply = load_module("terraform_apply", ".github/scripts/terraform_apply.py")


class TerraformCicdTests(unittest.TestCase):
    def test_safe_changes_omit_values_and_sensitive_attributes(self):
        plan = {"resource_changes": [{"address": 'aws_example.item["private-key"]', "mode": "managed", "change": {
            "actions": ["update"],
            "before": {"name": "old-secret", "removed": "value", "nested": {"password": "secret"}},
            "after": {"name": "new-secret", "added": "value", "nested": {"password": "new-secret"}},
            "before_sensitive": {"nested": {"password": True}},
            "after_sensitive": {"nested": {"password": True}},
            "after_unknown": {"computed": True},
        }}]}
        result = runner.safe_changes(plan)
        self.assertEqual(result["changes"][0]["address"], "aws_example.item[*]")
        self.assertEqual(result["changes"][0]["attribute_changes"], [
            {"name": "added", "change": "added"},
            {"name": "computed", "change": "unknown after apply"},
            {"name": "name", "change": "changed"},
            {"name": "removed", "change": "removed"},
        ])
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("nested", json.dumps(result))

    def test_approved_plan_expires_after_24_hours(self):
        binding = dict(repository="owner/repo", pr="1", head="a" * 40, base="b" * 40,
                       root="terraform/env/poc/example", run_id="1", attempt="1")
        manifest = dict(binding, created=100, expires=86500, sha256="a" * 64, plan_version="version")
        apply.validate_manifest(manifest, binding, now=86499)
        with self.assertRaises(ValueError):
            apply.validate_manifest(manifest, binding, now=86500)
        manifest["terraform_version"] = "1.16.6"
        with patch.dict(os.environ, {"TF_VERSION": "1.16.6"}), patch.object(runner.time, "time", return_value=86500):
            with self.assertRaisesRegex(ValueError, "Plan expired"):
                runner.verify_manifest(manifest, binding)

    def test_shell_rejects_expired_approved_plan(self):
        now = int(time.time())
        manifest = dict(repository="owner/repo", pr="7", head="a" * 40, base="b" * 40,
                        root="terraform/env/poc/example", run_id="2", attempt="1", created=now - 86401,
                        expires=now - 1, sha256="c" * 64, plan_version="version")
        script = r'''
    script_path="$1"; directory="$2"
    set -- check-config
    source "$script_path"
aws() { cp "$MANIFEST_FIXTURE" "${!#}"; printf '{"VersionId":"version"}\n'; }
    verify_approved_manifest terraform/env/poc/example '{"manifest_version":"version","sha256":"'"$(printf 'c%.0s' {1..64})"'"}' 2 1 "$directory"
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            environment = {**os.environ, "MANIFEST_FIXTURE": str(path), "TF_PLAN_ARTIFACT_BUCKET": "bucket",
                           "GITHUB_REPOSITORY": "owner/repo", "PR_NUMBER": "7", "EXPECTED_HEAD_SHA": "a" * 40,
                           "EXPECTED_BASE_SHA": "b" * 40, "AWS_ACCOUNT_ID": "123456789012", "AWS_REGION": "us-east-1",
                           "AWS_GITHUB_ACTIONS_ROLE_ARN": "arn:aws:iam::123456789012:role/test",
                           "TF_PIPELINE_NAME_PREFIX": "test", "TF_STATE_BUCKET": "state-bucket",
                           "TF_TEST_BUCKET": "test-bucket", "TARGET_ROOTS": '["terraform/env/poc/example"]'}
            result = subprocess.run(["bash", "-c", script, "test", str(ROOT / ".github/scripts/terraform_apply.sh"), directory],
                                    env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Saved plan manifest does not match", result.stderr)

    def test_summary_filters_values_and_instance_keys(self):
        result = {"changes": [{"address": 'aws_example.item["private]key"]', "actions": ["update"],
                               "attributes": ["name"], "attribute_changes": [
                                   {"name": "name", "change": "changed", "value": "secret"}]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            path.write_text(json.dumps(result), encoding="utf-8")
            output = subprocess.check_output(["bash", "-c", 'source "$1"; report_changes "$2"', "test",
                                              str(ROOT / ".github/scripts/terraform_plan.sh"), str(path)], text=True)
        self.assertIn("aws_example.item[*]", output)
        self.assertNotIn("private", output)
        self.assertNotIn("secret", output)

    def test_replan_does_not_read_old_plan_artifact(self):
        script = r'''
source "$1"
validate_pr_snapshot() { :; }
validate_pr_branch() { :; }
require_successful_plan() { printf '123\t1\n'; }
pipeline_name_for_root() { printf 'pipeline\n'; }
aws() { printf '%s\n' "$*" >&2; return 42; }
run_plan
'''
        environment = {**os.environ, "PR_NUMBER": "7", "PR_BRANCH": "branch", "EXPECTED_HEAD_SHA": "a" * 40,
                       "EXPECTED_BASE_SHA": "b" * 40, "TARGET_ROOTS": '["terraform/env/poc/example"]',
                       "REQUIRE_PR_PLAN": "true", "GITHUB_RUN_ID": "2", "GITHUB_RUN_ATTEMPT": "1"}
        result = subprocess.run(["bash", "-c", script, "test", str(ROOT / ".github/scripts/terraform_plan.sh")],
                                env=environment, capture_output=True, text=True)
        self.assertIn("codepipeline start-pipeline-execution", result.stderr)
        self.assertNotIn("s3api get-object", result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_cancel_only_old_runs_for_this_pr_without_active_apply(self):
        workflow = (ROOT / ".github/workflows/terraform-apply.yml").read_text(encoding="utf-8")
        block = workflow.split("          script: |\n", 1)[1].split("\n  inspect:", 1)[0]
        script = textwrap.dedent(block)
        harness = r'''
const script = process.env.TEST_SCRIPT;
process.env.PR_NUMBER = '7';
const runs = [
  {id: 10, event: 'pull_request_target', display_title: 'Terraform apply PR #7'},
  {id: 11, event: 'pull_request_target', display_title: 'Terraform apply PR #8'},
  {id: 12, event: 'pull_request_target', display_title: 'Terraform apply PR #7'},
  {id: 22, event: 'pull_request_target', display_title: 'Terraform apply PR #7'}
];
const cancelled = [];
const listWorkflowRuns = () => {};
const listJobsForWorkflowRun = () => {};
const github = {rest: {actions: {listWorkflowRuns, listJobsForWorkflowRun,
  cancelWorkflowRun: async ({run_id}) => cancelled.push(run_id)}},
  paginate: async (method, options) => method === listWorkflowRuns
    ? (options.status === 'in_progress' ? runs : [])
    : [{name: 'aws-apply', status: options.run_id === 12 ? 'in_progress' : 'waiting'}]};
const context = {repo: {owner: 'owner', repo: 'repo'}, runId: 20};
const core = {warning: () => {}};
new Function('github', 'context', 'core', `return (async () => {${script}})()`)(github, context, core)
  .then(() => { if (JSON.stringify(cancelled) !== '[10]') throw new Error(JSON.stringify(cancelled)); })
  .catch(error => { console.error(error); process.exitCode = 1; });
'''
        subprocess.run(["node", "-e", harness], env={**os.environ, "TEST_SCRIPT": script}, check=True)


if __name__ == "__main__":
    unittest.main()