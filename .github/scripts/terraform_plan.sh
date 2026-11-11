#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_PARENT="terraform/env/poc"
readonly PLAN_STATUS_CONTEXT="Terraform plan"
readonly SHARED_PATH_PREFIXES=("modules/" "terraform/modules/" "buildspec/")

pipeline_name_for_root() {
  local root="$1" prefix="${TF_PIPELINE_NAME_PREFIX:?}"
  [[ "$prefix" =~ ^[A-Za-z0-9.@_-]{1,60}$ ]] || fail "Invalid TF_PIPELINE_NAME_PREFIX repository variable."
  printf '%s-%s-apply\n' "$prefix" "${root##*/}"
}

fail() {
  printf 'Workflow failed: %s\n' "$1" >&2
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '## Workflow failed\n%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 1
}

discover_targets() {
  local parent="$1/$ROOT_PARENT"
  [[ -d "$parent" ]] || return 0
  local directory name
  while IFS= read -r -d '' directory; do
    name=${directory##*/}
    [[ "$name" =~ ^[A-Za-z0-9_-]+$ && "$name" != infra-cicd-* ]] || continue
    if find "$directory" -maxdepth 1 -type f -name '*.tf' -print -quit | grep -q .; then
      printf '%s/%s\n' "$ROOT_PARENT" "$name"
    fi
  done < <(find "$parent" -mindepth 1 -maxdepth 1 -type d ! -type l -print0)
}

validate_pr_snapshot() {
  local number="$1" expected_head="$2" expected_base="$3" attempt pr_json head base
  for attempt in 1 2 3; do
    pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the PR from GitHub."
    head=$(jq -r '.head.sha // empty' <<< "$pr_json")
    base=$(jq -r '.base.sha // empty' <<< "$pr_json")
    if [[ "$head" == "$expected_head" && "$base" == "$expected_base" ]] && jq -e \
      --arg repository "$GITHUB_REPOSITORY" \
      '.state == "open" and .draft == false and .mergeable == true
       and .head.repo.full_name == $repository and .base.repo.full_name == $repository
       and .base.ref == "main"' <<< "$pr_json" >/dev/null; then
      break
    fi
    if [[ "$head" != "$expected_head" || "$base" != "$expected_base" ]]; then
      fail "The PR head or base changed; run the workflow again."
    fi
    if [[ "$attempt" == 3 ]]; then
      fail "The PR must be open, ready for review, mergeable, and target main."
    fi
    sleep 2
  done
  local current_main
  current_main=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq '.object.sha') || fail "Could not read the current main SHA."
  [[ "$expected_base" == "$current_main" ]] || fail "The PR base is not the current main commit; retry after updating the PR."
}

validate_pr_branch() {
  local number="$1" branch="$2" owner="${GITHUB_REPOSITORY%%/*}" matches
  matches=$(gh api -X GET "repos/$GITHUB_REPOSITORY/pulls" -f state=open -f base=main -f "head=$owner:$branch") \
    || fail "Could not resolve the PR from its source branch."
  jq -e --arg number "$number" --arg branch "$branch" --arg repository "$GITHUB_REPOSITORY" \
    '[.[] | select((.number | tostring) == $number and .head.ref == $branch
      and .head.repo.full_name == $repository)] | length == 1' <<< "$matches" >/dev/null \
    || fail "The source branch does not identify exactly this open PR."
}

require_successful_plan() {
  local head="$1" base="$2" statuses status run_url run_id run
  statuses=$(gh api "repos/$GITHUB_REPOSITORY/commits/$head/status") || fail "Could not read the PR plan status."
  status=$(jq -c --arg context "$PLAN_STATUS_CONTEXT" '[.statuses[] | select(.context == $context)] | sort_by(.created_at) | last // empty' <<< "$statuses")
  [[ -n "$status" ]] || fail "No automatic Terraform plan status exists for the current PR head."
  jq -e --arg description "Plan passed for base $base" '.state == "success" and .description == $description' \
    <<< "$status" >/dev/null || fail "A successful plan for this exact PR head and base is required."
  run_url=$(jq -r '.target_url // empty' <<< "$status")
  [[ "$run_url" =~ ^https://github\.com/$GITHUB_REPOSITORY/actions/runs/([1-9][0-9]*)$ ]] || fail "The plan status has no trusted workflow run."
  run_id=${BASH_REMATCH[1]}
  run=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$run_id") || fail "Could not verify the plan workflow run."
  jq -e --arg head "$head" --arg repository "$GITHUB_REPOSITORY" \
    '.conclusion == "success" and .event == "pull_request" and .path == ".github/workflows/terraform-plan.yml"
     and .head_sha == $head and .repository.full_name == $repository' <<< "$run" >/dev/null \
    || fail "The successful plan is not from the expected workflow and PR head."
  printf '%s\t%s\n' "$(jq -r '.id' <<< "$run")" "$(jq -r '.run_attempt' <<< "$run")"
}

verify_previous_plan_manifest() {
  local root="$1" run_id="$2" attempt="$3" directory="$4" root_hash prefix response manifest_file
  root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
  prefix="plans/$GITHUB_REPOSITORY/pr-$PR_NUMBER/$EXPECTED_HEAD_SHA/$root_hash/$run_id/$attempt"
  manifest_file="$directory/previous-$root_hash.json"
  response="$directory/previous-response-$root_hash.json"
  aws s3api get-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" --key "$prefix/manifest.json" \
    "$manifest_file" > "$response"
  jq -e --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" --arg head "$EXPECTED_HEAD_SHA" \
    --arg base "$EXPECTED_BASE_SHA" --arg root "$root" --arg run "$run_id" --arg attempt "$attempt" \
    --argjson now "$(date +%s)" \
    '.repository == $repository and .pr == $pr and .head == $head and .base == $base and .root == $root
     and .run_id == $run and .attempt == $attempt and .created <= $now and $now < .expires
     and .expires <= .created + 86400 and (.sha256 | test("^[0-9a-f]{64}$"))
     and (.plan_version | type == "string" and length > 0)' "$manifest_file" >/dev/null \
    || fail "A valid, unexpired saved plan is required for $root."
}

select_targets() {
  local number="$1"
  local -a changed_files available_targets
  mapfile -t changed_files < <(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$number/files?per_page=100" \
    --jq '.[] | .filename, .previous_filename // empty')
  ((${#changed_files[@]} <= 3000)) || fail "The PR exceeds the GitHub changed-files API limit."
  mapfile -t available_targets < <(discover_targets "$GITHUB_WORKSPACE")
  local -A selected=()
  local path root name all_targets=false
  for path in "${changed_files[@]}"; do
    [[ "$path" == README.md || "$path" == terraform/README.md ]] && continue
    local shared=false prefix
    for prefix in "${SHARED_PATH_PREFIXES[@]}"; do
      if [[ "$path" == "$prefix"* ]]; then
        all_targets=true
        shared=true
        break
      fi
    done
    [[ "$shared" == true ]] && continue
    if [[ "$path" =~ ^terraform/env/poc/([A-Za-z0-9_-]+)/ ]]; then
      name=${BASH_REMATCH[1]}
      [[ "$name" != infra-cicd-* ]] || fail "CI/CD infrastructure changes are not automatic plan targets: $path"
      root="$ROOT_PARENT/$name"
      if [[ " ${available_targets[*]} " == *" $root "* || "$path" == *.tf ]]; then
        selected["$root"]=1
        continue
      fi
    fi
    fail "This PR includes a path outside the allowed Terraform roots: $path"
  done

  local target
  if [[ "$all_targets" == true ]]; then
    for target in "${available_targets[@]}"; do selected["$target"]=1; done
  fi
  ((${#selected[@]} > 0)) || fail "The PR has no registered Terraform target changes."
  local -a roots=()
  mapfile -t roots < <(printf '%s\n' "${!selected[@]}" | sort -u)
  jq -cn '$ARGS.positional' --args "${roots[@]}"
}

prepare_plan() {
  [[ "${GITHUB_EVENT_NAME:-}" == pull_request ]] || fail "Automatic planning requires a pull_request event."
  local number="${PR_NUMBER:?}" expected_head="${EVENT_PR_HEAD_SHA:?}" expected_base="${EVENT_PR_BASE_SHA:?}"
  validate_pr_snapshot "$number" "$expected_head" "$expected_base"

  local targets_json branch
  targets_json=$(select_targets "$number")
  branch=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number" --jq '.head.ref') || fail "Could not read the PR branch."
  {
    printf 'pr_number=%s\n' "$number"
    printf 'pr_branch=%s\n' "$branch"
    printf 'head_sha=%s\n' "$expected_head"
    printf 'base_sha=%s\n' "$expected_base"
    printf 'targets=%s\n' "$targets_json"
  } >> "$GITHUB_OUTPUT"
  gh api -X POST "repos/$GITHUB_REPOSITORY/statuses/$expected_head" \
    -f state=pending -f context="$PLAN_STATUS_CONTEXT" -f "description=Plan is running for base $expected_base" >/dev/null \
    || fail "Could not mark the PR commit status as pending."
  {
    printf '## Terraform plan\nPR: #%s\n\nCommit: `%s`\n\nTargets: %s\n' \
      "$number" "$expected_head" "$(IFS=', '; echo "${roots[*]}")"
  } >> "$GITHUB_STEP_SUMMARY"
}

validate_targets() {
  local candidate="${CANDIDATE_DIR:?}" root directory resolved
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
  for root in "${roots[@]}"; do
    [[ "$root" =~ ^terraform/env/poc/[A-Za-z0-9_-]+$ && "$root" != terraform/env/poc/infra-cicd-* ]] || fail "Invalid Terraform target root: $root"
    directory="$candidate/$root"
    [[ -d "$directory" && ! -L "$directory" ]] || fail "Terraform target root is missing or is a symlink: $root"
    resolved=$(realpath "$directory")
    [[ "$resolved" == "$directory" ]] || fail "Terraform target root resolves outside the candidate tree: $root"
    if find "$directory" -type l -print -quit | grep -q .; then fail "Symlinks are not allowed in a Terraform target: $root"; fi
    terraform -chdir="$directory" fmt -check -recursive
    terraform -chdir="$directory" init -backend=false -input=false -lockfile=readonly
    terraform -chdir="$directory" validate -no-color
  done
}

check_plan_config() {
  local account="${AWS_ACCOUNT_ID:?}" region="${AWS_REGION:?}" role="${AWS_GITHUB_PLAN_ROLE_ARN:?}"
  local prefix="${TF_PIPELINE_NAME_PREFIX:?}" bucket
  [[ "$account" =~ ^[0-9]{12}$ ]] || fail "Invalid AWS_ACCOUNT_ID secret."
  [[ "$region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || fail "Invalid AWS_REGION secret."
  [[ "$role" =~ ^arn:aws:iam::$account:role/[A-Za-z0-9_+=,.@/-]+$ ]] || fail "The plan role ARN does not match AWS_ACCOUNT_ID."
  [[ "$prefix" =~ ^[A-Za-z0-9.@_-]{1,60}$ ]] || fail "Invalid TF_PIPELINE_NAME_PREFIX repository variable."
  for bucket in "${TF_STATE_BUCKET:?}" "${TF_TEST_BUCKET:?}" "${TF_PLAN_ARTIFACT_BUCKET:?}"; do
    [[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "Invalid bucket name secret."
  done
}

verify_plan_manifest() {
  local root="$1" result_file="$2" manifest_file="$3" response_file="$4" run_id="$5" attempt="$6"
  local root_hash prefix manifest_version now
  root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
  prefix="plans/$GITHUB_REPOSITORY/pr-$PR_NUMBER/$EXPECTED_HEAD_SHA/$root_hash/$run_id/$attempt"
  aws s3api get-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" --key "$prefix/manifest.json" "$manifest_file" > "$response_file"
  manifest_version=$(jq -r '.VersionId // empty' "$response_file")
  [[ -n "$manifest_version" ]] || fail "The saved plan manifest has no S3 version."
  now=$(date +%s)
  jq -e \
    --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" --arg head "$EXPECTED_HEAD_SHA" \
    --arg base "$EXPECTED_BASE_SHA" --arg root "$root" --arg run "$run_id" --arg attempt "$attempt" \
    --arg version "$manifest_version" --arg result_sha "$(jq -r '.sha256 // empty' "$result_file")" \
    --arg result_version "$(jq -r '.manifest_version // empty' "$result_file")" --argjson now "$now" \
    '.repository == $repository and .pr == $pr and .head == $head and .base == $base and .root == $root
     and .run_id == $run and .attempt == $attempt and $version == $result_version
     and .created <= $now and $now < .expires and .expires <= .created + 86400
     and (.sha256 | test("^[0-9a-f]{64}$")) and .sha256 == $result_sha
     and (.plan_version | type == "string" and length > 0)' "$manifest_file" >/dev/null \
    || fail "The saved plan manifest does not match this PR execution."
  jq -cn --arg version "$manifest_version" --arg sha "$(jq -r '.sha256' "$manifest_file")" \
    --arg execution "$EXECUTION_ID" '{manifest_version:$version,sha256:$sha,execution_id:$execution}'
}

report_plan_result() {
  local root="$1" result_file="$2" build_status="$3"
  jq -e --arg root "$root" --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" \
    --arg head "$EXPECTED_HEAD_SHA" --arg base "$EXPECTED_BASE_SHA" --arg run "$GITHUB_RUN_ID" \
    --arg attempt "$GITHUB_RUN_ATTEMPT" \
    '.root == $root and .phase == "plan" and .status == "SUCCEEDED" and .repository == $repository
     and .pr == $pr and .head == $head and .base == $base and .run_id == $run and .attempt == $attempt' \
    "$result_file" >/dev/null || fail "CodeBuild returned an unverified plan result for $root."
  [[ "$build_status" == SUCCEEDED ]] || fail "Terraform CodeBuild failed for $root."
  local add change destroy
  add=$(jq -r '.counts.add // 0' "$result_file")
  change=$(jq -r '.counts.change // 0' "$result_file")
  destroy=$(jq -r '.counts.destroy // 0' "$result_file")
  {
    printf '### Terraform plan: %s\n\n' "$root"
    printf '| Added | Changed | Destroyed |\n| ---: | ---: | ---: |\n| %s | %s | %s |\n\n' "$add" "$change" "$destroy"
    printf 'Execution: `%s`\n\n' "$EXECUTION_ID"
  } >> "$GITHUB_STEP_SUMMARY"
}

run_plan() {
  local number="${PR_NUMBER:?}" head="${EXPECTED_HEAD_SHA:?}" base="${EXPECTED_BASE_SHA:?}"
  validate_pr_snapshot "$number" "$head" "$base"
  validate_pr_branch "$number" "${PR_BRANCH:?}"
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
  if [[ "${REQUIRE_PR_PLAN:-false}" == true ]]; then
    local prior_plan prior_run prior_attempt prior_directory
    prior_plan=$(require_successful_plan "$head" "$base")
    prior_run=${prior_plan%%$'\t'*}
    prior_attempt=${prior_plan#*$'\t'}
    prior_directory=$(mktemp -d)
    trap 'rm -rf "$prior_directory"' EXIT
    for root in "${roots[@]}"; do verify_previous_plan_manifest "$root" "$prior_run" "$prior_attempt" "$prior_directory"; done
    rm -rf "$prior_directory"
    trap - EXIT
  fi
  local root pipeline root_hash request_token variables_json execution attempt status execution_json revision
  local action_json build_id build_json log_group log_stream result_record result_file manifest_file manifest_response plans_json
  plans_json='{}'
  for root in "${roots[@]}"; do
    pipeline=$(pipeline_name_for_root "$root")
    root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
    request_token="gh-plan-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${root_hash:0:10}"
    variables_json=$(jq -cn --arg head "$head" --arg base "$base" --arg pr "$number" \
      --arg run "$GITHUB_RUN_ID" --arg attempt "$GITHUB_RUN_ATTEMPT" \
      '[{name:"PrCommitSha",value:$head},{name:"ExecutionMode",value:"plan"},{name:"PrNumber",value:$pr},
        {name:"BaseCommitSha",value:$base},{name:"PlanRunId",value:$run},{name:"PlanAttempt",value:$attempt},
        {name:"ManifestVersion",value:"UNSET"}]')
    execution=$(aws codepipeline start-pipeline-execution --name "$pipeline" --client-request-token "$request_token" \
      --source-revisions "actionName=Source,revisionType=COMMIT_ID,revisionValue=$head" --variables "$variables_json" \
      --query pipelineExecutionId --output text)
    EXECUTION_ID="$execution"
    printf 'Started plan for %s: %s\n' "$root" "$execution" >> "$GITHUB_STEP_SUMMARY"

    status=InProgress
    for attempt in $(seq 1 160); do
      execution_json=$(aws codepipeline get-pipeline-execution --pipeline-name "$pipeline" \
        --pipeline-execution-id "$execution" --output json)
      status=$(jq -r '.pipelineExecution.status' <<< "$execution_json")
      [[ "$status" == InProgress || "$status" == Stopping ]] || break
      sleep 15
    done
    [[ "$status" == Succeeded ]] || fail "CodePipeline for $root ended with status $status."
    revision=$(jq -r '.pipelineExecution.artifactRevisions[]? | select(.name == "SourceOutput") | .revisionId' <<< "$execution_json" | tail -n 1)
    [[ "$revision" == "$head" ]] || fail "CodePipeline source revision does not match the requested PR head for $root."
    validate_pr_snapshot "$number" "$head" "$base"

    action_json=$(aws codepipeline list-action-executions --pipeline-name "$pipeline" \
      --filter "pipelineExecutionId=$execution" --output json)
    build_id=$(jq -r '[.actionExecutionDetails[] | select(.actionName == "TerraformPlan" and .stageName == "Plan")
      | .output.executionResult.externalExecutionId // empty] | last // empty' <<< "$action_json")
    [[ -n "$build_id" ]] || fail "TerraformPlan CodeBuild execution was not found for $root."
    build_json=$(aws codebuild batch-get-builds --ids "$build_id" --output json)
    log_group=$(jq -r '.builds[0].logs.groupName // empty' <<< "$build_json")
    log_stream=$(jq -r '.builds[0].logs.streamName // empty' <<< "$build_json")
    [[ -n "$log_group" && -n "$log_stream" ]] || fail "CodeBuild log stream is unavailable for $root."

    result_record=''
    local next_token='' next_forward_token page page_records
    for attempt in $(seq 1 200); do
      if [[ -n "$next_token" ]]; then
        page=$(aws logs get-log-events --log-group-name "$log_group" --log-stream-name "$log_stream" \
          --start-from-head --next-token "$next_token" --output json)
      else
        page=$(aws logs get-log-events --log-group-name "$log_group" --log-stream-name "$log_stream" \
          --start-from-head --output json)
      fi
      page_records=$(jq -r '.events[]?.message | select(startswith("TF_RESULT_JSON:")) | ltrimstr("TF_RESULT_JSON:")' <<< "$page")
      [[ -z "$page_records" ]] || result_record=$(tail -n 1 <<< "$page_records")
      next_forward_token=$(jq -r '.nextForwardToken // empty' <<< "$page")
      [[ -n "$next_forward_token" && "$next_forward_token" != "$next_token" ]] || break
      next_token="$next_forward_token"
    done
    [[ -n "$result_record" ]] || fail "Structured Terraform result was not found for $root."

    local temporary_directory
    temporary_directory=$(mktemp -d)
    result_file="$temporary_directory/result.json"
    manifest_file="$temporary_directory/manifest.json"
    manifest_response="$temporary_directory/response.json"
    printf '%s\n' "$result_record" > "$result_file"
    report_plan_result "$root" "$result_file" "$(jq -r '.builds[0].buildStatus // empty' <<< "$build_json")"
    local plan_binding_json
    plan_binding_json=$(verify_plan_manifest "$root" "$result_file" "$manifest_file" "$manifest_response" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT")
    plans_json=$(jq -cn --argjson existing "$plans_json" --arg root "$root" --argjson binding "$plan_binding_json" '$existing + {($root):$binding}')
    rm -rf "$temporary_directory"
  done
  validate_pr_snapshot "$number" "$head" "$base"
  printf 'planned_head=%s\nplanned_base=%s\nplans=%s\n' "$head" "$base" "$plans_json" >> "$GITHUB_OUTPUT"
}

finalize_plan_status() {
  local head="${PLAN_HEAD_SHA:-}" base="${PLAN_BASE_SHA:-}" state description
  [[ "$head" =~ ^[0-9a-f]{40}$ && "$base" =~ ^[0-9a-f]{40}$ ]] || return 0
  if [[ "${PREFLIGHT_RESULT:-}" == success && "${STATIC_CHECK_RESULT:-}" == success && "${PLAN_RESULT:-}" == success ]]; then
    state=success
    description="Plan passed for base $base"
  else
    state=failure
    description="Automatic Terraform plan failed; review the workflow run"
  fi
  gh api -X POST "repos/$GITHUB_REPOSITORY/statuses/$head" \
    -f "state=$state" -f context="$PLAN_STATUS_CONTEXT" -f "description=$description" \
    -f "target_url=${PLAN_RUN_URL:?}" >/dev/null || fail "Could not publish the final Terraform plan status."
  printf 'Terraform plan status for `%s`: **%s**\n' "$head" "$state" >> "$GITHUB_STEP_SUMMARY"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    prepare-plan) prepare_plan ;;
    validate) validate_targets ;;
    check-plan-config) check_plan_config ;;
    plan) run_plan ;;
    finalize-plan-status) finalize_plan_status ;;
    *) fail "Unknown command: ${1:-}" ;;
  esac
fi