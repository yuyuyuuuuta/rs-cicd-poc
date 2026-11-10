import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


TARGETS = {"terraform/env/poc/ci-test"}
DOC_PATHS = {"README.md", "terraform/README.md"}
PLAN_STATUS_CONTEXT = "Terraform plan"


def validate_pr(pr, repository, expected_head=None, expected_base=None):
    if pr.get("state") != "open" or pr.get("draft"):
        raise ValueError("The PR must be open and ready for review.")
    if (pr.get("head", {}).get("repo") or {}).get("full_name") != repository:
        raise ValueError("Fork PRs cannot run an AWS apply.")
    if (pr.get("base", {}).get("repo") or {}).get("full_name") != repository:
        raise ValueError("The PR base repository does not match.")
    if pr.get("base", {}).get("ref") != "main":
        raise ValueError("The PR must target main.")
    if pr.get("mergeable") is not True:
        raise ValueError("The PR is conflicting or GitHub is still calculating mergeability; retry later.")
    head = pr.get("head", {}).get("sha", "")
    base = pr.get("base", {}).get("sha", "")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (head, base)):
        raise ValueError("The PR contains an invalid commit SHA.")
    if expected_head is not None and head != expected_head:
        raise ValueError("The PR head changed; run the workflow again.")
    if expected_base is not None and base != expected_base:
        raise ValueError("The PR base changed; run the workflow again.")
    return head, base


def select_targets(paths):
    selected = set()
    for path in paths:
        if path in DOC_PATHS:
            continue
        matches = [root for root in TARGETS if path.startswith(root + "/")]
        if not matches:
            raise ValueError(f"This PR includes a path outside the allowed target roots: {path}")
        selected.update(matches)
    if not selected:
        raise ValueError("The PR has no registered Terraform target changes; no apply or merge will run.")
    return sorted(selected)


def extract_terraform_log(messages, root, phase):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", messages)
    lines = text.splitlines()
    begin = f"=== TERRAFORM_{phase.upper()}_BEGIN:{root} ==="
    end = f"=== TERRAFORM_{phase.upper()}_END:{root} ==="
    try:
        start = lines.index(begin) + 1
        stop = lines.index(end, start)
    except ValueError as error:
        raise ValueError("Terraform apply log markers were not found.") from error
    result = []
    for line in lines[start:stop]:
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", line)
        if line.strip() or (result and result[-1]):
            result.append(line.rstrip())
    return "\n".join(result).strip()


def extract_apply_log(messages, root):
    return extract_terraform_log(messages, root, "apply")


def fenced_log(text, limit=60000):
    text = redact_identifiers(text)
    if len(text) > limit:
        text = text[: limit // 4] + "\n[log truncated]\n" + text[-(limit * 3 // 4) :]
    fence_length = max([3] + [len(match) + 1 for match in re.findall(r"`+", text)])
    fence = "`" * fence_length
    return f"{fence}text\n{text}\n{fence}\n"


def output(name, value):
    text = json.dumps(value, separators=(",", ":")) if isinstance(value, (list, dict)) else str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("Workflow output must be a single line.")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as destination:
        destination.write(f"{name}={text}\n")


def summary(text):
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as destination:
        destination.write(redact_identifiers(text) + "\n")


def pipeline_names():
    return parse_pipeline_names(os.environ["TF_PIPELINE_NAMES"], "TF_PIPELINE_NAMES")


def plan_pipeline_names():
    return parse_pipeline_names(os.environ["TF_PLAN_PIPELINE_NAMES"], "TF_PLAN_PIPELINE_NAMES")


def parse_pipeline_names(value, setting_name):
    names = json.loads(value)
    if not isinstance(names, dict) or set(names) != TARGETS:
        raise ValueError(f"{setting_name} must map every registered root to its Pipeline name.")
    if not all(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9.@_-]{1,100}", name) for name in names.values()):
        raise ValueError(f"Invalid Pipeline name in {setting_name}.")
    return names


def redact_identifiers(text):
    values = {
        os.environ.get(name, "")
        for name in (
            "AWS_ACCOUNT_ID", "AWS_REGION", "TF_STATE_BUCKET", "TF_TEST_BUCKET", "TF_PLAN_ARTIFACT_BUCKET",
            "AWS_GITHUB_ACTIONS_ROLE_ARN", "AWS_GITHUB_PLAN_ROLE_ARN",
        )
    }
    try:
        for env_name in ("TF_PIPELINE_NAMES", "TF_PLAN_PIPELINE_NAMES"):
            names = json.loads(os.environ.get(env_name, "{}"))
            if isinstance(names, dict):
                values.update(name for name in names.values() if isinstance(name, str))
    except ValueError:
        pass
    text = re.sub(r"arn:(?:aws|aws-us-gov|aws-cn):[^\s`\"<>]+", "[REDACTED_ARN]", text)
    for value in sorted((value for value in values if value), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?<!\d)\d{12}(?!\d)", "[REDACTED_ACCOUNT]", text)
    return re.sub(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b", "[REDACTED_ID]", text)


class GitHub:
    def __init__(self):
        self.repository = os.environ["GITHUB_REPOSITORY"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid repository name.")
        self.token = os.environ["GH_TOKEN"]

    def request(self, path, method="GET", data=None):
        body = json.dumps(data).encode() if data is not None else None
        request = Request(
            f"https://api.github.com/repos/{self.repository}/{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "rs-cicd-poc",
            },
        )
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub {method} {path} failed with HTTP {error.code}.") from error

    def files(self, number, expected_count):
        if expected_count > 3000:
            raise ValueError("The PR exceeds the GitHub changed-files API limit.")
        files = []
        for page in range(1, 31):
            batch = self.request(f"pulls/{number}/files?per_page=100&page={page}")
            files.extend(batch)
            if len(batch) < 100:
                break
        if len(files) != expected_count:
            raise ValueError("Could not retrieve the complete PR changed-file list.")
        paths = []
        for changed in files:
            paths.append(changed["filename"])
            if changed.get("previous_filename"):
                paths.append(changed["previous_filename"])
        return paths


class Aws:
    def call(self, service, operation, **arguments):
        command = [
            "aws", "--region", os.environ["AWS_REGION"], "--no-cli-pager", "--no-paginate",
            "--cli-connect-timeout", "10", "--cli-read-timeout", "60", service, operation,
            "--output", "json",
        ]
        for name, value in arguments.items():
            command.append("--" + name.replace("_", "-"))
            if value is True:
                continue
            command.append(json.dumps(value) if isinstance(value, (dict, list)) else str(value))
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if result.returncode:
            raise RuntimeError(f"AWS {service} {operation} failed: {result.stderr.strip()[:2000]}")
        return json.loads(result.stdout)


def pr_number():
    number = os.environ["PR_NUMBER"]
    if not re.fullmatch(r"[1-9][0-9]*", number):
        raise ValueError("PR_NUMBER must be a positive integer.")
    return number


def target_roots():
    roots = json.loads(os.environ["TARGET_ROOTS"])
    if not isinstance(roots, list) or not roots or any(root not in TARGETS for root in roots):
        raise ValueError("Unregistered Terraform target roots.")
    if len(roots) != len(set(roots)):
        raise ValueError("Duplicate target roots.")
    return roots


def pr_branch():
    branch = os.environ["PR_BRANCH"]
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch)
        or branch.startswith("-")
        or branch.startswith("/")
        or branch.endswith("/")
        or ".." in branch
        or "//" in branch
    ):
        raise ValueError("PR_BRANCH must be a valid source branch name.")
    return branch


def find_open_pr_by_branch(github, branch):
    owner = github.repository.split("/", 1)[0]
    head = quote(f"{owner}:{branch}", safe="")
    pull_requests = github.request(f"pulls?state=open&base=main&head={head}&per_page=100")
    matches = [
        pr for pr in pull_requests
        if pr.get("head", {}).get("ref") == branch
        and (pr.get("head", {}).get("repo") or {}).get("full_name") == github.repository
    ]
    if len(matches) != 1:
        raise ValueError("The source branch must identify exactly one open, same-repository PR targeting main.")
    return matches[0]


def latest_plan_status(github, head_sha):
    response = github.request(f"commits/{head_sha}/status")
    matches = [item for item in response.get("statuses", []) if item.get("context") == PLAN_STATUS_CONTEXT]
    if not matches:
        raise ValueError("No automatic Terraform plan status exists for the current PR head.")
    return max(matches, key=lambda item: item.get("created_at", ""))


def require_successful_plan(github, head_sha, base_sha):
    status = latest_plan_status(github, head_sha)
    expected_description = f"Plan passed for base {base_sha}"
    if status.get("state") != "success" or status.get("description") != expected_description:
        raise ValueError("A successful automatic plan for the current PR head and base is required before apply.")


def set_plan_status(github, head_sha, state, description, target_url=None):
    payload = {
        "state": state,
        "context": PLAN_STATUS_CONTEXT,
        "description": description[:140],
    }
    if target_url:
        payload["target_url"] = target_url
    return github.request(f"statuses/{head_sha}", method="POST", data=payload)


def snapshot(github, number, expected_head=None, expected_base=None):
    pr = github.request(f"pulls/{number}")
    head, base = validate_pr(pr, github.repository, expected_head, expected_base)
    current_main = github.request("git/ref/heads/main")["object"]["sha"]
    if base != current_main:
        raise ValueError("The PR base is not the current main commit; retry later.")
    return pr, head, base


def prepare_plan():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
        raise ValueError("Automatic planning must use the trusted pull_request_target workflow.")
    github = GitHub()
    number = pr_number()
    pr, head, base = snapshot(
        github,
        number,
        expected_head=os.environ["EVENT_PR_HEAD_SHA"],
        expected_base=os.environ["EVENT_PR_BASE_SHA"],
    )
    roots = select_targets(github.files(number, pr["changed_files"]))
    set_plan_status(github, head, "pending", f"Plan is running for base {base}")
    output("pr_number", number)
    output("head_sha", head)
    output("base_sha", base)
    output("targets", roots)
    output("pr_branch", pr["head"]["ref"])
    summary(f"## Terraform plan\nPR: #{number}\n\nCommit: `{head}`\n\nTargets: {', '.join(roots)}")


def prepare_apply():
    if os.environ["GITHUB_REF"] != "refs/heads/main":
        raise ValueError("Dispatch this workflow from main; select the PR source branch in the input.")
    github = GitHub()
    pr = find_open_pr_by_branch(github, pr_branch())
    number = str(pr["number"])
    head, base = validate_pr(pr, github.repository)
    current_main = github.request("git/ref/heads/main")["object"]["sha"]
    if base != current_main:
        raise ValueError("The PR base is not the current main commit; update the PR and wait for a new plan.")
    roots = select_targets(github.files(number, pr["changed_files"]))
    require_successful_plan(github, head, base)
    output("pr_number", number)
    output("head_sha", head)
    output("base_sha", base)
    output("targets", roots)
    output("pr_branch", pr["head"]["ref"])
    summary(f"## Terraform apply preflight\nPR: #{number}\n\nBranch: `{pr['head']['ref']}`\n\nCommit: `{head}`\n\nTargets: {', '.join(roots)}")


def validate():
    candidate = Path(os.environ["CANDIDATE_DIR"]).resolve()
    for root in target_roots():
        directory = candidate / root
        if not directory.is_dir() or directory.resolve() != directory:
            raise ValueError("The target root is missing or resolves through a symlink.")
        if any(path.is_symlink() for path in directory.rglob("*")):
            raise ValueError("Symlinks are not allowed in an apply target.")
        for arguments in (
            ["fmt", "-check", "-recursive"],
            ["init", "-backend=false", "-input=false", "-lockfile=readonly"],
            ["validate", "-no-color"],
        ):
            subprocess.run(["terraform", f"-chdir={directory}", *arguments], check=True)


def wait_pipeline(aws, pipeline, execution_id, timeout=2400, pause=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while clock() < deadline:
        execution = aws.call(
            "codepipeline", "get-pipeline-execution",
            pipeline_name=pipeline, pipeline_execution_id=execution_id,
        )["pipelineExecution"]
        if execution["pipelineExecutionId"] != execution_id:
            raise ValueError("The returned Pipeline execution ID does not match.")
        if execution["status"] not in ("InProgress", "Stopping"):
            return execution
        pause(15)
    raise TimeoutError("Pipeline wait timed out. AWS execution may still be running; do not merge.")


def read_log_events(aws, group, stream):
    token = None
    messages = []
    size = 0
    for _ in range(200):
        arguments = {"log_group_name": group, "log_stream_name": stream, "start_from_head": True}
        if token:
            arguments["next_token"] = token
        page = aws.call("logs", "get-log-events", **arguments)
        for event in page.get("events", []):
            size += len(event["message"])
            if size > 2000000:
                raise ValueError("CloudWatch Logs exceeded the retrieval size limit.")
            messages.append(event["message"])
        next_token = page.get("nextForwardToken")
        if not next_token or next_token == token:
            return "\n".join(messages)
        token = next_token
    raise ValueError("CloudWatch Logs exceeded the pagination limit.")


def report_execution(aws, pipeline, execution_id, root, phase="apply"):
    actions = []
    token = None
    for _ in range(100):
        arguments = {"pipeline_name": pipeline, "filter": {"pipelineExecutionId": execution_id}}
        if token:
            arguments["next_token"] = token
        page = aws.call("codepipeline", "list-action-executions", **arguments)
        actions.extend(page.get("actionExecutionDetails", []))
        token = page.get("nextToken")
        if not token:
            break
    else:
        raise ValueError("Pipeline action pagination limit exceeded.")
    action_name = "TerraformPlan" if phase == "plan" else "TerraformApply"
    stage_name = "Plan" if phase == "plan" else "Apply"
    builds = [
        action for action in actions
        if action.get("actionName") == action_name and action.get("stageName") == stage_name
    ]
    if not builds:
        raise ValueError(f"No {action_name} CodeBuild action was found; inspect the Source stage.")
    action = max(builds, key=lambda item: item.get("startTime", ""))
    build_id = action.get("output", {}).get("executionResult", {}).get("externalExecutionId")
    if not build_id:
        raise ValueError("The TerraformApply action has no CodeBuild execution ID.")
    build = aws.call("codebuild", "batch-get-builds", ids=[build_id])["builds"][0]
    group = build.get("logs", {}).get("groupName")
    stream = build.get("logs", {}).get("streamName")
    if not group or not stream:
        raise ValueError("The CodeBuild log group or stream is unavailable.")
    for attempt in range(3):
        messages = read_log_events(aws, group, stream)
        try:
            text = extract_terraform_log(messages, root, phase)
            break
        except ValueError:
            if attempt == 2:
                summary(f"### Terraform {phase} log markers missing\n" + fenced_log("\n".join(messages.splitlines()[-40:])))
                raise
            time.sleep(2)
    summary(f"### Terraform {phase}: {root}\nBuild: `{build_id}`\n\n" + fenced_log(text))
    if build.get("buildStatus") != "SUCCEEDED":
        raise ValueError(f"CodeBuild did not succeed: {build.get('buildStatus')}")


def execute_pipelines(phase):
    github = GitHub()
    aws = Aws()
    number = pr_number()
    pr = find_open_pr_by_branch(github, pr_branch())
    if str(pr["number"]) != number:
        raise ValueError("The PR resolved from the source branch changed before execution.")
    head = os.environ["EXPECTED_HEAD_SHA"]
    base = os.environ["EXPECTED_BASE_SHA"]
    names = plan_pipeline_names() if phase == "plan" else pipeline_names()
    snapshot(github, number, head, base)
    if phase == "apply":
        require_successful_plan(github, head, base)
    for root in target_roots():
        snapshot(github, number, head, base)
        if phase == "apply":
            require_successful_plan(github, head, base)
        pipeline = names[root]
        target_hash = hashlib.sha256(root.encode()).hexdigest()[:10]
        request_token = f"gh-{phase}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}-{target_hash}"
        execution_id = aws.call(
            "codepipeline", "start-pipeline-execution", name=pipeline,
            client_request_token=request_token,
            source_revisions=[{"actionName": "Source", "revisionType": "COMMIT_ID", "revisionValue": head}],
            variables=[{"name": "PrCommitSha", "value": head}],
        )["pipelineExecutionId"]
        summary(f"## AWS Terraform {phase}\nRoot: `{root}`\n\nExecution: `{execution_id}`")
        failure = None
        try:
            execution = wait_pipeline(aws, pipeline, execution_id)
            summary(f"Result: **{execution['status']}**")
            if execution["status"] != "Succeeded":
                raise ValueError(f"Pipeline ended with status {execution['status']}.")
            revisions = execution.get("artifactRevisions", [])
            if not any(item.get("name") == "SourceOutput" and item.get("revisionId") == head for item in revisions):
                raise ValueError("Pipeline source artifact revision does not match the requested PR SHA.")
        except Exception as error:
            failure = error
            summary("Pipeline completion or revision verification failed; automatic merge is blocked.")
        try:
            report_execution(aws, pipeline, execution_id, root, phase)
        except Exception as error:
            summary("Log retrieval or build verification failed; automatic merge is blocked.")
            if failure is None:
                failure = error
        if failure is not None:
            raise failure
    snapshot(github, number, head, base)
    if phase == "apply":
        output("applied_head", head)
        output("applied_base", base)
    else:
        output("planned_head", head)
        output("planned_base", base)


def plan():
    execute_pipelines("plan")


def apply():
    execute_pipelines("apply")


def finalize_plan_status():
    head = os.environ.get("PLAN_HEAD_SHA", "")
    base = os.environ.get("PLAN_BASE_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", head) or not re.fullmatch(r"[0-9a-f]{40}", base):
        return
    success = all(
        os.environ.get(name) == "success"
        for name in ("PREFLIGHT_RESULT", "STATIC_CHECK_RESULT", "PLAN_RESULT")
    )
    state = "success" if success else "failure"
    description = f"Plan passed for base {base}" if success else "Automatic Terraform plan failed; review the workflow run"
    set_plan_status(GitHub(), head, state, description, os.environ.get("PLAN_RUN_URL"))
    summary(f"Terraform plan status for `{head}`: **{state}**")


def merge():
    github = GitHub()
    head = os.environ["EXPECTED_HEAD_SHA"]
    pr = find_open_pr_by_branch(github, pr_branch())
    number = str(pr["number"])
    snapshot(github, number, head, os.environ["EXPECTED_BASE_SHA"])
    require_successful_plan(github, head, os.environ["EXPECTED_BASE_SHA"])
    result = github.request(
        f"pulls/{number}/merge", method="PUT", data={"sha": head, "merge_method": "merge"},
    )
    if not result.get("merged"):
        raise ValueError("GitHub rejected the merge; branch protection is not bypassed.")
    summary(f"## PR merged\nPR: #{number}\n\nApplied commit: `{head}`\n\nMerge commit: `{result['sha']}`")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=("prepare-plan", "prepare-apply", "validate", "plan", "finalize-plan-status", "apply", "merge", "check-config", "check-plan-config"),
    )
    command = parser.parse_args().command
    try:
        {
            "prepare-plan": prepare_plan,
            "prepare-apply": prepare_apply,
            "validate": validate,
            "plan": plan,
            "finalize-plan-status": finalize_plan_status,
            "apply": apply,
            "merge": merge,
            "check-config": check_config,
            "check-plan-config": check_plan_config,
        }[command]()
    except Exception as error:
        print(redact_identifiers(f"Workflow failed: {error}"), file=sys.stderr)
        return 1
    return 0


def check_config():
    required = ("AWS_ACCOUNT_ID", "AWS_REGION", "TF_STATE_BUCKET", "TF_TEST_BUCKET", "AWS_GITHUB_ACTIONS_ROLE_ARN", "TF_PIPELINE_NAMES")
    if any(not os.environ.get(name) for name in required):
        raise ValueError("Required GitHub Secrets are missing; see the configuration guide.")
    account = os.environ["AWS_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Invalid AWS_ACCOUNT_ID Secret.")
    role = os.environ["AWS_GITHUB_ACTIONS_ROLE_ARN"]
    if not re.fullmatch(r"arn:aws:iam::" + account + r":role/[A-Za-z0-9_+=,.@/-]+", role):
        raise ValueError("The OIDC role ARN does not match the configured account.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", os.environ["AWS_REGION"]):
        raise ValueError("Invalid AWS_REGION Secret.")
    for name in ("TF_STATE_BUCKET", "TF_TEST_BUCKET"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", os.environ[name]):
            raise ValueError("Invalid bucket name Secret.")
    pipeline_names()


def check_plan_config():
    required = (
        "AWS_ACCOUNT_ID", "AWS_REGION", "TF_STATE_BUCKET", "TF_TEST_BUCKET", "TF_PLAN_ARTIFACT_BUCKET",
        "AWS_GITHUB_PLAN_ROLE_ARN", "TF_PLAN_PIPELINE_NAMES",
    )
    if any(not os.environ.get(name) for name in required):
        raise ValueError("Required GitHub plan configuration is missing; see the configuration guide.")
    account = os.environ["AWS_ACCOUNT_ID"]
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Invalid AWS_ACCOUNT_ID secret.")
    role = os.environ["AWS_GITHUB_PLAN_ROLE_ARN"]
    if not re.fullmatch(r"arn:aws:iam::" + account + r":role/[A-Za-z0-9_+=,.@/-]+", role):
        raise ValueError("The plan role ARN does not match the configured account.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", os.environ["AWS_REGION"]):
        raise ValueError("Invalid AWS_REGION secret.")
    for name in ("TF_STATE_BUCKET", "TF_TEST_BUCKET", "TF_PLAN_ARTIFACT_BUCKET"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", os.environ[name]):
            raise ValueError(f"Invalid {name} secret.")
    plan_pipeline_names()


if __name__ == "__main__":
    sys.exit(main())