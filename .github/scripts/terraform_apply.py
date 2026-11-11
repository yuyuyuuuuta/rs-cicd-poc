import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


TARGET_ROOT_PARENT = "terraform/env/poc"
TARGET_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
DEPENDENCIES = {}
SHARED_PATH_PREFIXES = ("modules/", "terraform/modules/", "buildspec/")
DOC_PATHS = {"README.md", "terraform/README.md"}
PLAN_STATUS_CONTEXT = "Terraform plan"


def validate_manifest(manifest, expected, now=None):
    for name in ("repository", "pr", "head", "base", "root", "run_id", "attempt"):
        if manifest.get(name) != expected.get(name):
            raise ValueError(f"Saved plan {name} does not match the approved execution.")
    now = int(time.time()) if now is None else now
    created = manifest.get("created", 0)
    expires = manifest.get("expires", 0)
    if not isinstance(created, int) or not isinstance(expires, int) or not created <= now < expires <= created + 86400:
        raise ValueError("Saved plan is expired or has an invalid lifetime.")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest.get("sha256", "")) or not manifest.get("plan_version"):
        raise ValueError("Saved plan has no valid digest or S3 version.")
    return manifest


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
    repository_root = Path(__file__).resolve().parents[2]
    targets = discover_targets(repository_root)
    selected = set()
    added_targets = set()
    all_targets = False

    for path in paths:
        if path in DOC_PATHS:
            continue
        if path.startswith(SHARED_PATH_PREFIXES):
            all_targets = True
            continue
        match = re.match(r"^terraform/env/poc/([A-Za-z0-9_-]+)/", path)
        if not match or match[1].startswith("infra-cicd-"):
            raise ValueError(f"This PR includes a path outside the allowed target roots: {path}")
        root = f"{TARGET_ROOT_PARENT}/{match[1]}"
        if root in targets or path.endswith(".tf"):
            added_targets.add(root)
        else:
            raise ValueError(f"This PR includes a path outside the allowed target roots: {path}")

    if all_targets:
        selected.update(targets)
        selected.update(added_targets)
    else:
        selected.update(added_targets)

    def dependencies(prefix, visited):
        if prefix in visited:
            return set()
        visited.add(prefix)
        result = {prefix}
        for dependency in DEPENDENCIES.get(prefix, set()):
            result.update(dependencies(dependency, visited))
        return result

    selected = set().union(*(dependencies(root, set()) for root in selected)) if selected else set()
    if not selected:
        raise ValueError("The PR has no registered Terraform target changes; no apply or merge will run.")
    return sorted(selected)


def discover_targets(repository_root=None):
    repository_root = Path(repository_root or Path(__file__).resolve().parents[2])
    parent = repository_root / TARGET_ROOT_PARENT
    if not parent.is_dir():
        return set()
    return {
        f"{TARGET_ROOT_PARENT}/{directory.name}"
        for directory in parent.iterdir()
        if directory.is_dir()
        and not directory.is_symlink()
        and not directory.name.startswith("infra-cicd-")
        and TARGET_NAME_PATTERN.fullmatch(directory.name)
        and any(item.is_file() and item.suffix == ".tf" for item in directory.iterdir())
    }


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


def pipeline_names(required_targets=()):
    prefix = os.environ["TF_PIPELINE_NAME_PREFIX"]
    if not re.fullmatch(r"[A-Za-z0-9.@_-]{1,60}", prefix):
        raise ValueError("Invalid TF_PIPELINE_NAME_PREFIX repository variable.")
    return {root: f"{prefix}-{Path(root).name}-apply" for root in required_targets}


def redact_identifiers(text):
    values = {
        os.environ.get(name, "")
        for name in (
            "AWS_ACCOUNT_ID", "AWS_REGION", "TF_STATE_BUCKET", "TF_TEST_BUCKET", "TF_PLAN_ARTIFACT_BUCKET",
            "AWS_GITHUB_ACTIONS_ROLE_ARN", "AWS_GITHUB_PLAN_ROLE_ARN", "TF_PIPELINE_NAME_PREFIX",
        )
    }
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
            try:
                message = json.load(error).get("message", "Request rejected")
            except (ValueError, AttributeError):
                message = "Request rejected"
            raise RuntimeError(f"GitHub {method} {path} failed with HTTP {error.code}: {message}") from error

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
        outfile = arguments.pop("outfile", None)
        for name, value in arguments.items():
            command.append("--" + name.replace("_", "-"))
            if value is True:
                continue
            command.append(json.dumps(value) if isinstance(value, (dict, list)) else str(value))
        if outfile:
            command.append(str(outfile))
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
    root_pattern = re.compile(r"^terraform/env/poc/(?!infra-cicd-)[A-Za-z0-9_-]+$")
    if not isinstance(roots, list) or not roots or any(not isinstance(root, str) or not root_pattern.fullmatch(root) for root in roots):
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
    match = re.fullmatch(r"https://github\.com/" + re.escape(github.repository) + r"/actions/runs/([1-9][0-9]*)", status.get("target_url", ""))
    if not match:
        raise ValueError("The plan status has no trusted workflow execution.")
    run = github.request(f"actions/runs/{match[1]}")
    if (run.get("conclusion") != "success" or run.get("event") != "pull_request_target"
            or run.get("path") != ".github/workflows/terraform-plan.yml" or run.get("head_sha") != head_sha
            or (run.get("repository") or {}).get("full_name") != github.repository):
        raise ValueError("The plan workflow did not succeed from the expected trusted base.")
    return str(run["id"]), str(run["run_attempt"])


def label_event(github):
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as source:
        event = json.load(source)
    if (os.environ.get("GITHUB_EVENT_NAME") != "pull_request_target" or event.get("action") != "labeled"
            or event.get("label", {}).get("name") != "apply-request" or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("Apply must start from an apply-request label event targeting main.")
    actors = {event["sender"]["login"], os.environ.get("GITHUB_TRIGGERING_ACTOR", event["sender"]["login"])}
    for actor in actors:
        permission = github.request(f"collaborators/{quote(actor, safe='')}/permission").get("permission")
        if permission not in ("write", "maintain", "admin"):
            raise ValueError("The label sender and rerun actor must have repository write permission.")
    return event


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
    for attempt in range(3):
        if pr.get("mergeable") is not None:
            break
        time.sleep(2)
        pr = github.request(f"pulls/{number}")
    head, base = validate_pr(pr, github.repository, expected_head, expected_base)
    current_main = github.request("git/ref/heads/main")["object"]["sha"]
    if base != current_main:
        raise ValueError("The PR base is not the current main commit; retry later.")
    return pr, head, base


def prepare_plan():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request_target":
        raise ValueError("Automatic planning must use the pull_request_target workflow.")
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
    github = GitHub()
    event = label_event(github)
    number = str(event["pull_request"]["number"])
    pr, head, base = snapshot(github, number, event["pull_request"]["head"]["sha"], event["pull_request"]["base"]["sha"])
    if "apply-request" not in {label["name"] for label in pr.get("labels", [])}:
        raise ValueError("The apply-request label was removed.")
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
    result = None
    for attempt in range(3):
        messages = read_log_events(aws, group, stream)
        records = [line.split("TF_RESULT_JSON:", 1)[1] for line in messages.splitlines() if line.startswith("TF_RESULT_JSON:")]
        if records:
            result = json.loads(records[-1])
            break
        if attempt < 2:
            time.sleep(2)
    if not result or result.get("root") != root or result.get("phase") != phase:
        summary(f"### {phase}: {root}\nNo verified result record. Inspect restricted AWS diagnostics.")
        raise ValueError("No matching structured Terraform result was found.")
    counts = result.get("counts", {})
    summary(f"### {phase}: {root}\nResult: **{result.get('status')}**\n\n"
            "| Added | Changed | Destroyed |\n| ---: | ---: | ---: |\n"
            f"| {counts.get('add', '-')} | {counts.get('change', '-')} | {counts.get('destroy', '-')} |\n")
    rows = []
    for row in result.get("changes", []):
        safe_row = {name: row[name] for name in ("actions", "attributes") if name in row}
        safe_row["address"] = re.sub(r'\[(?:"(?:\\.|[^"\\])*"|[0-9]+)\]', '[*]', row["address"])
        safe_row["attribute_changes"] = [{name: attribute[name] for name in ("name", "change") if name in attribute}
                                         for attribute in row.get("attribute_changes", [])]
        rows.append(safe_row)
    summary(fenced_log(json.dumps(rows, indent=2), limit=20000))
    if result.get("error"):
        summary("Error summary:\n" + fenced_log(result["error"], limit=2000))
    summary("Execution log: source verified; backend initialized; Terraform validated; " + phase + " " + result.get("status", "FAILED")
            + ". Values and raw diagnostics are withheld. [CloudWatch Logs](https://console.aws.amazon.com/cloudwatch/home#logsV2:log-groups)")
    if build.get("buildStatus") != "SUCCEEDED" or result.get("status") != "SUCCEEDED":
        raise ValueError(f"CodeBuild did not succeed: {build.get('buildStatus')}")
    return result


def plan_binding(root, run_id=None, attempt=None):
    return dict(repository=os.environ["GITHUB_REPOSITORY"], pr=pr_number(), head=os.environ["EXPECTED_HEAD_SHA"],
                base=os.environ["EXPECTED_BASE_SHA"], root=root, run_id=run_id or os.environ["GITHUB_RUN_ID"],
                attempt=attempt or os.environ["GITHUB_RUN_ATTEMPT"])


def plan_prefix(binding):
    root_hash = hashlib.sha256(binding["root"].encode()).hexdigest()[:16]
    return f"plans/{binding['repository']}/pr-{binding['pr']}/{binding['head']}/{root_hash}/{binding['run_id']}/{binding['attempt']}"


def read_manifest(aws, binding, version=None):
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "manifest.json"
        arguments = dict(bucket=os.environ["TF_PLAN_ARTIFACT_BUCKET"], key=plan_prefix(binding) + "/manifest.json", outfile=path)
        if version:
            arguments["version_id"] = version
        response = aws.call("s3api", "get-object", **arguments)
        manifest = validate_manifest(json.loads(path.read_text(encoding="utf-8")), binding)
    return manifest, response["VersionId"]


def authorize_plan(aws, binding, saved):
    if binding["run_id"] != os.environ["GITHUB_RUN_ID"] or binding["attempt"] != os.environ["GITHUB_RUN_ATTEMPT"]:
        raise ValueError("Reruns require a new pre-approval plan and a new Environment approval.")
    manifest, version = read_manifest(aws, binding, saved["manifest_version"])
    if saved["sha256"] != manifest["sha256"] or saved["manifest_version"] != version:
        raise ValueError("The approved plan digest or version changed.")
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "approval.json"
        path.write_text(json.dumps(dict(binding=binding, manifest_version=version, sha256=manifest["sha256"])), encoding="utf-8")
        aws.call("s3api", "put-object", bucket=os.environ["TF_PLAN_ARTIFACT_BUCKET"],
                 key="approvals/" + plan_prefix(binding) + ".json", body=str(path), server_side_encryption="AES256", if_none_match="*")


def execute_pipelines(phase):
    github = GitHub()
    aws = Aws()
    number = pr_number()
    pr = find_open_pr_by_branch(github, pr_branch())
    if str(pr["number"]) != number:
        raise ValueError("The PR resolved from the source branch changed before execution.")
    head = os.environ["EXPECTED_HEAD_SHA"]
    base = os.environ["EXPECTED_BASE_SHA"]
    roots = target_roots()
    names = pipeline_names(roots)
    snapshot(github, number, head, base)
    if phase == "apply":
        label_event(github)
    saved_plans = json.loads(os.environ["SAVED_PLANS"]) if phase == "apply" else {}
    if phase == "apply" and set(saved_plans) != set(roots):
        raise ValueError("Saved plans do not cover exactly the selected roots.")
    if phase == "apply" or os.environ.get("REQUIRE_PR_PLAN") == "true":
        require_successful_plan(github, head, base)
    failures = []
    results = {}
    for root in roots:
        current, _, _ = snapshot(github, number, head, base)
        if phase == "apply" and "apply-request" not in {label["name"] for label in current.get("labels", [])}:
            raise ValueError("The apply-request label was removed.")
        pipeline = names[root]
        binding = plan_binding(root)
        target_hash = hashlib.sha256(root.encode()).hexdigest()[:10]
        request_token = f"gh-{phase}-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}-{target_hash}"
        execution_id = None
        failure = None
        try:
            if phase == "apply":
                authorize_plan(aws, binding, saved_plans[root])
            variables = dict(PrCommitSha=head, ExecutionMode=phase, PrNumber=number, BaseCommitSha=base,
                             PlanRunId=binding["run_id"], PlanAttempt=binding["attempt"],
                             ManifestVersion=saved_plans[root]["manifest_version"] if phase == "apply" else "UNSET")
            execution_id = aws.call(
            "codepipeline", "start-pipeline-execution", name=pipeline,
            client_request_token=request_token,
            source_revisions=[{"actionName": "Source", "revisionType": "COMMIT_ID", "revisionValue": head}],
            variables=[{"name": name, "value": value} for name, value in variables.items()],
        )["pipelineExecutionId"]
            summary(f"## AWS Terraform {phase}\nPR: #{number}\n\nCommit: `{head}`\n\nRoot: `{root}`\n\nExecution: `{execution_id}`")
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
        if execution_id:
            try:
                result = report_execution(aws, pipeline, execution_id, root, phase)
                if any(result.get(name) != expected for name, expected in binding.items()):
                    raise ValueError("Build result is not bound to the requested PR and plan execution.")
                if phase == "plan" and failure is None:
                    manifest, version = read_manifest(aws, binding, result["manifest_version"])
                    if manifest["sha256"] != result["sha256"]:
                        raise ValueError("Plan result and manifest digest differ.")
                    results[root] = dict(manifest_version=version, sha256=manifest["sha256"], execution_id=execution_id)
            except Exception as error:
                summary("Log retrieval or build verification failed; automatic merge is blocked.")
                if failure is None:
                    failure = error
        if failure is not None:
            failures.append(f"{root}: {failure}")
    if failures:
        raise ValueError("One or more modules failed: " + "; ".join(failures))
    snapshot(github, number, head, base)
    if phase == "apply":
        output("applied_head", head)
        output("applied_base", base)
    else:
        output("planned_head", head)
        output("planned_base", base)
        output("plans", results)


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
    if number != pr_number():
        raise ValueError("The branch now identifies a different PR; automatic merge is blocked.")
    current, _, _ = snapshot(github, number, head, os.environ["EXPECTED_BASE_SHA"])
    if "apply-request" not in {label["name"] for label in current.get("labels", [])}:
        raise ValueError("The apply-request label was removed.")
    if current.get("mergeable_state") not in ("clean", "unstable"):
        raise ValueError("PR mergeability is blocked; no bypass will be attempted.")
    require_successful_plan(github, head, os.environ["EXPECTED_BASE_SHA"])
    result = github.request(
        f"pulls/{number}/merge", method="PUT", data={"sha": head, "merge_method": "merge"},
    )
    if not result.get("merged"):
        raise ValueError("GitHub rejected the merge: " + result.get("message", "branch protection or mergeability requirements were not met"))
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
        summary("## Workflow failed\n" + fenced_log(str(error), limit=4000))
        print(redact_identifiers(f"Workflow failed: {error}"), file=sys.stderr)
        return 1
    return 0


def check_config():
    required = ("AWS_ACCOUNT_ID", "AWS_REGION", "TF_STATE_BUCKET", "TF_TEST_BUCKET", "TF_PLAN_ARTIFACT_BUCKET", "AWS_GITHUB_ACTIONS_ROLE_ARN", "TF_PIPELINE_NAME_PREFIX")
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
    pipeline_names(discover_targets())


def check_plan_config():
    required = (
        "AWS_ACCOUNT_ID", "AWS_REGION", "TF_STATE_BUCKET", "TF_TEST_BUCKET", "TF_PLAN_ARTIFACT_BUCKET",
        "AWS_GITHUB_PLAN_ROLE_ARN", "TF_PIPELINE_NAME_PREFIX",
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
    roots = json.loads(os.environ["TARGET_ROOTS"]) if os.environ.get("TARGET_ROOTS") else discover_targets()
    pipeline_names(roots)


if __name__ == "__main__":
    sys.exit(main())