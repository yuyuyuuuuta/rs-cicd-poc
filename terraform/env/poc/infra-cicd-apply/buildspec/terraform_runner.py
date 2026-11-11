import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen


def aws(operation, **arguments):
    command = ["aws", "s3api", operation, "--output", "json", "--no-cli-pager"]
    outfile = arguments.pop("outfile", None)
    for name, value in arguments.items():
        command.extend(["--" + name.replace("_", "-"), str(value)])
    if outfile:
        command.append(outfile)
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return json.loads(result.stdout or "{}")


def put_json(bucket, key, value, directory, exclusive=False):
    path = directory / "upload.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    arguments = dict(bucket=bucket, key=key, body=str(path), server_side_encryption="AES256")
    if exclusive:
        arguments["if_none_match"] = "*"
    return aws("put-object", **arguments)["VersionId"]


def get_json(bucket, key, directory, version=None):
    path = directory / "download.json"
    arguments = dict(bucket=bucket, key=key)
    if version:
        arguments["version_id"] = version
    response = aws("get-object", **arguments, **{"outfile": str(path)})
    return json.loads(path.read_text(encoding="utf-8")), response["VersionId"]


def safe_changes(plan):
    counts = dict(add=0, change=0, destroy=0)
    changes = []
    for resource in plan.get("resource_changes", []):
        if resource.get("mode") == "data":
            continue
        change = resource["change"]
        actions = change["actions"]
        if actions == ["no-op"]:
            continue
        counts["add"] += int("create" in actions)
        counts["change"] += int("update" in actions)
        counts["destroy"] += int("delete" in actions)
        before, after = change.get("before") or {}, change.get("after") or {}
        sensitive = set()
        for field in ("before_sensitive", "after_sensitive"):
            marked = change.get(field, {})
            if marked is True:
                sensitive.update(set(before) | set(after))
            elif isinstance(marked, dict):
                sensitive.update(name for name, value in marked.items() if value)
        attributes = sorted(name for name in set(before) | set(after)
                            if name not in sensitive and before.get(name) != after.get(name))
        changes.append(dict(address=resource["address"], actions=actions, attributes=attributes))
    return dict(counts=counts, changes=changes)


def binding():
    names = dict(repository="GITHUB_REPOSITORY_NAME", pr="PR_NUMBER", head="EXPECTED_COMMIT_SHA",
                 base="BASE_COMMIT_SHA", root="TARGET_STATE_DIR", run_id="PLAN_RUN_ID", attempt="PLAN_ATTEMPT")
    value = {name: os.environ[variable] for name, variable in names.items()}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise ValueError("Invalid repository")
    if not all(re.fullmatch(r"[1-9][0-9]*", value[name]) for name in ("pr", "run_id", "attempt")):
        raise ValueError("Invalid execution identity")
    if not all(re.fullmatch(r"[0-9a-f]{40}", value[name]) for name in ("head", "base")):
        raise ValueError("Invalid commit")
    if value["head"] != os.environ["SOURCE_COMMIT_SHA"]:
        raise ValueError("Source revision mismatch")
    root = value["root"]
    if not re.fullmatch(r"terraform/env/poc/[A-Za-z0-9_-]+", root) or root.startswith("terraform/env/poc/infra-cicd-"):
        raise ValueError("Invalid root")
    return value


def plan_prefix(value):
    root_hash = hashlib.sha256(value["root"].encode()).hexdigest()[:16]
    return f"plans/{value['repository']}/pr-{value['pr']}/{value['head']}/{root_hash}/{value['run_id']}/{value['attempt']}"


def verify_manifest(manifest, value):
    if any(manifest.get(name) != expected for name, expected in value.items()):
        raise ValueError("Plan identity mismatch")
    now = int(time.time())
    if not manifest["created"] <= now < manifest["expires"] <= manifest["created"] + 86400:
        raise ValueError("Plan expired")
    if manifest["terraform_version"] != os.environ["TF_VERSION"]:
        raise ValueError("Terraform version mismatch")


def fresh_pr(value, mode):
    def request(path):
        request = Request(f"https://api.github.com/repos/{value['repository']}/{path}",
                          headers={"Accept": "application/vnd.github+json", "User-Agent": "terraform-cicd"})
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    pr = request(f"pulls/{value['pr']}")
    if (pr.get("state") != "open" or pr.get("draft") or pr["head"]["sha"] != value["head"]
            or pr["base"]["sha"] != value["base"] or pr["base"]["ref"] != "main"
            or (pr["head"].get("repo") or {}).get("full_name") != value["repository"]
            or request("git/ref/heads/main")["object"]["sha"] != value["base"]):
        raise ValueError("PR changed while the pipeline was queued")
    workflow = request(f"actions/runs/{value['run_id']}")
    plan_workflow = ".github/workflows/terraform-plan.yml"
    apply_workflow = ".github/workflows/terraform-apply.yml"
    workflow_path = workflow.get("path")
    if workflow_path == plan_workflow:
        expected_event = "pull_request_target"
    elif workflow_path == apply_workflow:
        expected_event = "pull_request_target"
    else:
        raise ValueError("Unexpected originating workflow")
    if (workflow.get("status") != "in_progress" or str(workflow.get("run_attempt")) != value["attempt"]
            or workflow.get("head_sha") != value["head"] or workflow.get("event") != expected_event):
        raise ValueError("The originating workflow is no longer active")
    if mode == "apply" and workflow_path != apply_workflow:
        raise ValueError("Unexpected originating workflow")
    if workflow_path == apply_workflow and "apply-request" not in {label["name"] for label in pr.get("labels", [])}:
        raise ValueError("The apply label was removed")


def run():
    value = binding()
    mode = os.environ["EXECUTION_MODE"]
    if mode not in ("plan", "apply") or mode != os.environ["BUILD_MODE"]:
        raise ValueError("Execution mode is not permitted by this project")
    source = Path(os.environ["CODEBUILD_SRC_DIR"]).resolve()
    target = source / value["root"]
    if not target.is_dir() or target.resolve() != target or any(path.is_symlink() for path in target.rglob("*")):
        raise ValueError("Invalid target directory")
    bucket = os.environ["PLAN_BUCKET"]
    prefix = plan_prefix(value)
    result = dict(phase=mode, status="FAILED", **value)
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        private_log = directory / "terraform.log"
        private_log.touch()

        def terraform(*arguments, accepted=(0,)):
            completed = subprocess.run(["terraform", f"-chdir={target}", *arguments], capture_output=True, text=True)
            with private_log.open("a", encoding="utf-8") as destination:
                destination.write(completed.stdout + completed.stderr)
            if completed.returncode not in accepted:
                if "Saved plan is stale" in completed.stderr:
                    reason = "STALE_PLAN: recreate the plan and obtain a new approval"
                elif "Error acquiring the state lock" in completed.stderr:
                    reason = "STATE_LOCK: another operation holds the Terraform state lock"
                else:
                    reason = "TERRAFORM_" + arguments[0].upper() + "_FAILED: consult restricted AWS diagnostics"
                raise ValueError(reason)
            return completed

        try:
            fresh_pr(value, mode)
            if mode == "apply":
                manifest, manifest_version = get_json(bucket, prefix + "/manifest.json", directory,
                                                      os.environ["MANIFEST_VERSION"])
                verify_manifest(manifest, value)
                approval, _ = get_json(bucket, "approvals/" + prefix + ".json", directory)
                if (approval.get("manifest_version") != manifest_version
                        or approval.get("sha256") != manifest["sha256"]
                        or approval.get("binding") != value):
                    raise ValueError("Approval does not match the saved plan")
                plan = directory / "approved.tfplan"
                aws("get-object", bucket=bucket, key=prefix + "/plan.tfplan",
                    version_id=manifest["plan_version"], outfile=str(plan))
                if hashlib.sha256(plan.read_bytes()).hexdigest() != manifest["sha256"]:
                    raise ValueError("Plan digest mismatch")
                put_json(bucket, "consumed/" + prefix + ".json",
                         dict(binding=value, execution=os.environ["PIPELINE_EXECUTION_ID"]), directory, exclusive=True)
            terraform("init", "-input=false", "-lockfile=readonly",
                      f"-backend-config=bucket={os.environ['STATE_BUCKET']}",
                      f"-backend-config=key={os.environ['STATE_KEY']}",
                      f"-backend-config=region={os.environ['AWS_DEFAULT_REGION']}",
                      "-backend-config=encrypt=true", "-backend-config=use_lockfile=true")
            terraform("validate", "-no-color")
            if mode == "plan":
                plan = directory / "saved.tfplan"
                terraform("plan", "-input=false", "-no-color", "-lock-timeout=5m", "-detailed-exitcode",
                          f"-out={plan}", accepted=(0, 2))
                rendered = terraform("show", "-json", str(plan))
                changes = safe_changes(json.loads(rendered.stdout))
                version = aws("put-object", bucket=bucket, key=prefix + "/plan.tfplan", body=str(plan),
                              server_side_encryption="AES256")["VersionId"]
                created = int(time.time())
                manifest = dict(value, created=created, expires=created + 86400, plan_version=version,
                                terraform_version=os.environ["TF_VERSION"],
                                sha256=hashlib.sha256(plan.read_bytes()).hexdigest(), **changes)
                manifest_version = put_json(bucket, prefix + "/manifest.json", manifest, directory, exclusive=True)
                result.update(manifest_version=manifest_version, sha256=manifest["sha256"], **changes)
            else:
                fresh_pr(value, mode)
                root_hash = hashlib.sha256(value["root"].encode()).hexdigest()[:16]
                put_json(bucket, f"consumed/by-head/{value['repository']}/pr-{value['pr']}/{value['head']}/{root_hash}.json",
                         dict(binding=value, execution=os.environ["PIPELINE_EXECUTION_ID"]), directory, exclusive=True)
                completed = terraform("apply", "-input=false", "-no-color", "-lock-timeout=5m", str(plan))
                matched = re.search(r"Apply complete! Resources: (\d+) added, (\d+) changed, (\d+) destroyed\.", completed.stdout)
                if not matched:
                    raise ValueError("Apply completed without a verifiable resource summary")
                counts = dict(zip(("add", "change", "destroy"), map(int, matched.groups())))
                result.update(counts=counts, changes=manifest["changes"])
            result["status"] = "SUCCEEDED"
        except ValueError as error:
            result["error"] = str(error)
            with private_log.open("a", encoding="utf-8") as destination:
                destination.write(f"{type(error).__name__}: {error}\n")
            raise
        except Exception as error:
            result["error"] = "AWS_OR_TERRAFORM_EXECUTION_FAILED: consult restricted AWS diagnostics"
            with private_log.open("a", encoding="utf-8") as destination:
                destination.write(f"{type(error).__name__}: {error}\n")
                stderr = getattr(error, "stderr", None)
                if stderr:
                    destination.write(stderr)
            raise
        finally:
            if private_log.exists():
                aws("put-object", bucket=bucket, key="diagnostics/" + prefix + f"/{mode}-{os.environ['PIPELINE_EXECUTION_ID']}.log",
                    body=str(private_log), server_side_encryption="AES256")
            print("TF_RESULT_JSON:" + json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print("Terraform execution refused or failed. Review the restricted AWS execution diagnostics.", flush=True)
        raise SystemExit(1)