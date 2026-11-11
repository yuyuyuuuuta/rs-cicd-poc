#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/terraform_plan.sh"

APPLY_TEMP_DIRECTORY=""
cleanup_apply_temp_directory() {
  [[ -z "$APPLY_TEMP_DIRECTORY" ]] || rm -rf "$APPLY_TEMP_DIRECTORY"
}
trap cleanup_apply_temp_directory EXIT

validate_apply_label_event() {
  local event_file="${GITHUB_EVENT_PATH:?}" event sender actor permission
  [[ "${GITHUB_EVENT_NAME:-}" == pull_request_target && "${GITHUB_REF:-}" == refs/heads/main ]] \
    || fail "Apply must start from a pull_request_target event targeting main."
  event=$(jq -c . "$event_file") || fail "Could not parse the GitHub event payload."
  jq -e '.action == "labeled" and .label.name == "apply-request"' <<< "$event" >/dev/null \
    || fail "Apply must start when the apply-request label is added."
  sender=$(jq -r '.sender.login // empty' <<< "$event")
  actor="${GITHUB_TRIGGERING_ACTOR:-$sender}"
  [[ -n "$sender" && -n "$actor" ]] || fail "Could not identify the label sender and workflow actor."
  for actor in "$sender" "${GITHUB_TRIGGERING_ACTOR:-$sender}"; do
    permission=$(gh api "repos/$GITHUB_REPOSITORY/collaborators/$actor/permission" --jq '.permission') \
      || fail "Could not verify repository permission for $actor."
    [[ "$permission" == write || "$permission" == maintain || "$permission" == admin ]] \
      || fail "The label sender and workflow actor must have repository write permission."
  done
}

prepare_apply() {
  validate_apply_label_event
  local event number head base pr_json roots plan_run
  event=$(jq -c . "${GITHUB_EVENT_PATH:?}")
  number=$(jq -r '.pull_request.number' <<< "$event")
  head=$(jq -r '.pull_request.head.sha' <<< "$event")
  base=$(jq -r '.pull_request.base.sha' <<< "$event")
  validate_pr_snapshot "$number" "$head" "$base"
  pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the current PR."
  jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
    || fail "The apply-request label was removed."
  plan_run=$(require_successful_plan "$head" "$base")
  roots=$(select_targets "$number")
  local branch
  branch=$(jq -r '.head.ref' <<< "$pr_json")
  validate_pr_branch "$number" "$branch"
  {
    printf 'pr_number=%s\n' "$number"
    printf 'pr_branch=%s\n' "$branch"
    printf 'head_sha=%s\n' "$head"
    printf 'base_sha=%s\n' "$base"
    printf 'targets=%s\n' "$roots"
  } >> "$GITHUB_OUTPUT"
  {
    printf '## Terraform apply preflight\nPR: #%s\n\nBranch: `%s`\n\nCommit: `%s`\n\nTargets: %s\n\nPlan run: `%s`\n' \
      "$number" "$branch" "$head" "$(jq -r 'join(", ")' <<< "$roots")" "${plan_run%%$'\t'*}"
  } >> "$GITHUB_STEP_SUMMARY"
}

check_apply_config() {
  local account="${AWS_ACCOUNT_ID:?}" region="${AWS_REGION:?}" role="${AWS_GITHUB_ACTIONS_ROLE_ARN:?}"
  local prefix="${TF_PIPELINE_NAME_PREFIX:?}" bucket
  [[ "$account" =~ ^[0-9]{12}$ ]] || fail "Invalid AWS_ACCOUNT_ID secret."
  [[ "$region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || fail "Invalid AWS_REGION secret."
  [[ "$role" =~ ^arn:aws:iam::$account:role/[A-Za-z0-9_+=,.@/-]+$ ]] || fail "The apply role ARN does not match AWS_ACCOUNT_ID."
  [[ "$prefix" =~ ^[A-Za-z0-9.@_-]{1,60}$ ]] || fail "Invalid TF_PIPELINE_NAME_PREFIX repository variable."
  for bucket in "${TF_STATE_BUCKET:?}" "${TF_TEST_BUCKET:?}" "${TF_PLAN_ARTIFACT_BUCKET:?}"; do
    [[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "Invalid bucket name secret."
  done
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
}

verify_approved_manifest() {
  local root="$1" saved="$2" run_id="$3" attempt="$4" directory="$5"
  local version sha root_hash prefix response manifest_file
  version=$(jq -r '.manifest_version // empty' <<< "$saved")
  sha=$(jq -r '.sha256 // empty' <<< "$saved")
  [[ -n "$version" && "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "Saved plan reference is missing its S3 version or digest for $root."
  root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
  prefix="plans/$GITHUB_REPOSITORY/pr-$PR_NUMBER/$EXPECTED_HEAD_SHA/$root_hash/$run_id/$attempt"
  manifest_file="$directory/manifest-$root_hash.json"
  response="$directory/manifest-response-$root_hash.json"
  aws s3api get-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" --key "$prefix/manifest.json" \
    --version-id "$version" "$manifest_file" > "$response"
  jq -e --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" --arg head "$EXPECTED_HEAD_SHA" \
    --arg base "$EXPECTED_BASE_SHA" --arg root "$root" --arg run "$run_id" --arg attempt "$attempt" \
    --arg sha "$sha" --arg version "$version" --argjson now "$(date +%s)" \
    '.repository == $repository and .pr == $pr and .head == $head and .base == $base and .root == $root
    and .run_id == $run and .attempt == $attempt and .sha256 == $sha
    and (.sha256 | test("^[0-9a-f]{64}$"))
     and .created <= $now and $now < .expires and .expires <= .created + 86400
     and (.plan_version | type == "string" and length > 0)' "$manifest_file" >/dev/null \
    || fail "Saved plan manifest does not match the approved PR execution for $root."
  [[ "$(jq -r '.VersionId // empty' "$response")" == "$version" ]] || fail "Saved plan S3 version changed for $root."
  jq -cn --argjson binding "$(jq -cn --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" \
    --arg head "$EXPECTED_HEAD_SHA" --arg base "$EXPECTED_BASE_SHA" --arg root "$root" \
    --arg run "$run_id" --arg attempt "$attempt" \
    '{repository:$repository,pr:$pr,head:$head,base:$base,root:$root,run_id:$run,attempt:$attempt}')" \
    --arg version "$version" --arg sha "$sha" '{binding:$binding,manifest_version:$version,sha256:$sha}'
}

wait_for_pipeline() {
  local pipeline="$1" execution="$2" attempt status execution_json
  status=InProgress
  for attempt in $(seq 1 160); do
    execution_json=$(aws codepipeline get-pipeline-execution --pipeline-name "$pipeline" \
      --pipeline-execution-id "$execution" --output json)
    status=$(jq -r '.pipelineExecution.status' <<< "$execution_json")
    [[ "$status" == InProgress || "$status" == Stopping ]] || break
    sleep 15
  done
  [[ "$status" == Succeeded ]] || fail "CodePipeline ended with status $status."
  printf '%s' "$execution_json"
}

read_build_result() {
  local pipeline="$1" execution="$2" action_name="$3" stage_name="$4" result_file="$5"
  local action_json build_id build_json log_group log_stream next_token='' next_forward_token page records attempt
  action_json=$(aws codepipeline list-action-executions --pipeline-name "$pipeline" \
    --filter "pipelineExecutionId=$execution" --output json)
  build_id=$(jq -r --arg action "$action_name" --arg stage "$stage_name" \
    '[.actionExecutionDetails[] | select(.actionName == $action and .stageName == $stage)
      | .output.executionResult.externalExecutionId // empty] | last // empty' <<< "$action_json")
  [[ -n "$build_id" ]] || fail "$action_name CodeBuild execution was not found."
  build_json=$(aws codebuild batch-get-builds --ids "$build_id" --output json)
  log_group=$(jq -r '.builds[0].logs.groupName // empty' <<< "$build_json")
  log_stream=$(jq -r '.builds[0].logs.streamName // empty' <<< "$build_json")
  [[ -n "$log_group" && -n "$log_stream" ]] || fail "CodeBuild log stream is unavailable."
  for attempt in $(seq 1 200); do
    if [[ -n "$next_token" ]]; then
      page=$(aws logs get-log-events --log-group-name "$log_group" --log-stream-name "$log_stream" \
        --start-from-head --next-token "$next_token" --output json)
    else
      page=$(aws logs get-log-events --log-group-name "$log_group" --log-stream-name "$log_stream" \
        --start-from-head --output json)
    fi
    records=$(jq -r '.events[]?.message | select(startswith("TF_RESULT_JSON:")) | ltrimstr("TF_RESULT_JSON:")' <<< "$page")
    if [[ -n "$records" ]]; then
      printf '%s\n' "$(tail -n 1 <<< "$records")" > "$result_file"
      jq -cn --arg result_file "$result_file" --arg status "$(jq -r '.builds[0].buildStatus // empty' <<< "$build_json")" \
        '{result_file:$result_file,build_status:$status}'
      return 0
    fi
    next_forward_token=$(jq -r '.nextForwardToken // empty' <<< "$page")
    [[ -n "$next_forward_token" && "$next_forward_token" != "$next_token" ]] || break
    next_token="$next_forward_token"
  done
  fail "Structured Terraform result was not found in CodeBuild logs."
}

run_apply() {
  local number="${PR_NUMBER:?}" head="${EXPECTED_HEAD_SHA:?}" base="${EXPECTED_BASE_SHA:?}"
  validate_apply_label_event
  validate_pr_snapshot "$number" "$head" "$base"
  validate_pr_branch "$number" "${PR_BRANCH:?}"
  local pr_json
  pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the current PR."
  jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
    || fail "The apply-request label was removed."
  require_successful_plan "$head" "$base" >/dev/null
  local -a roots
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
  jq -e --argjson roots "$(jq -cn '$ARGS.positional' --args "${roots[@]}")" \
    'keys | sort == ($roots | sort)' <<< "${SAVED_PLANS:?}" >/dev/null \
    || fail "Saved plans do not cover exactly the selected roots."

  APPLY_TEMP_DIRECTORY=$(mktemp -d)
  local root pipeline saved approved root_hash request_token variables_json approval_file execution execution_json revision
  local result_info result_file build_status
  for root in "${roots[@]}"; do
    validate_pr_snapshot "$number" "$head" "$base"
    pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the current PR."
    jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
      || fail "The apply-request label was removed."
    pipeline=$(pipeline_name_for_root "$root")
    saved=$(jq -c --arg root "$root" '.[$root]' <<< "$SAVED_PLANS")
    approved=$(verify_approved_manifest "$root" "$saved" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$APPLY_TEMP_DIRECTORY")
    approval_file="$APPLY_TEMP_DIRECTORY/approval.json"
    printf '%s\n' "$approved" > "$approval_file"
    root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-10)
    request_token="gh-apply-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${root_hash}"
    variables_json=$(jq -cn --arg head "$head" --arg base "$base" --arg pr "$number" \
      --arg run "$GITHUB_RUN_ID" --arg attempt "$GITHUB_RUN_ATTEMPT" \
      --arg version "$(jq -r '.manifest_version' <<< "$saved")" \
      '[{name:"PrCommitSha",value:$head},{name:"ExecutionMode",value:"apply"},{name:"PrNumber",value:$pr},
        {name:"BaseCommitSha",value:$base},{name:"PlanRunId",value:$run},{name:"PlanAttempt",value:$attempt},
        {name:"ManifestVersion",value:$version}]')
    aws s3api put-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" \
      --key "approvals/plans/$GITHUB_REPOSITORY/pr-$number/$head/$(printf '%s' "$root" | sha256sum | cut -c1-16)/$GITHUB_RUN_ID/$GITHUB_RUN_ATTEMPT.json" \
      --body "$approval_file" --server-side-encryption AES256 --if-none-match '*' >/dev/null
    execution=$(aws codepipeline start-pipeline-execution --name "$pipeline" --client-request-token "$request_token" \
      --source-revisions "actionName=Source,revisionType=COMMIT_ID,revisionValue=$head" --variables "$variables_json" \
      --query pipelineExecutionId --output text)
    printf '## AWS Terraform apply\nPR: #%s\n\nCommit: `%s`\n\nRoot: `%s`\n\nExecution: `%s`\n' \
      "$number" "$head" "$root" "$execution" >> "$GITHUB_STEP_SUMMARY"
    execution_json=$(wait_for_pipeline "$pipeline" "$execution")
    revision=$(jq -r '.pipelineExecution.artifactRevisions[]? | select(.name == "SourceOutput") | .revisionId' <<< "$execution_json" | tail -n 1)
    [[ "$revision" == "$head" ]] || fail "CodePipeline source revision does not match the requested PR head for $root."
    validate_pr_snapshot "$number" "$head" "$base"
    result_file="$APPLY_TEMP_DIRECTORY/apply-$root_hash.json"
    result_info=$(read_build_result "$pipeline" "$execution" TerraformApply Apply "$result_file")
    build_status=$(jq -r '.build_status' <<< "$result_info")
    jq -e --arg root "$root" --arg repository "$GITHUB_REPOSITORY" --arg pr "$number" \
      --arg head "$head" --arg base "$base" --arg run "$GITHUB_RUN_ID" --arg attempt "$GITHUB_RUN_ATTEMPT" \
      '.root == $root and .phase == "apply" and .status == "SUCCEEDED" and .repository == $repository
       and .pr == $pr and .head == $head and .base == $base and .run_id == $run and .attempt == $attempt' \
      "$result_file" >/dev/null || fail "CodeBuild returned an unverified apply result for $root."
    [[ "$build_status" == SUCCEEDED ]] || fail "Terraform CodeBuild failed for $root."
    jq -r --arg root "$root" --arg execution "$execution" \
      '"### Apply result: " + $root + "\n\n| Added | Changed | Destroyed |\n| ---: | ---: | ---: |\n| " + (.counts.add|tostring) + " | " + (.counts.change|tostring) + " | " + (.counts.destroy|tostring) + " |\n\nExecution: `" + $execution + "`\n"' \
      "$result_file" >> "$GITHUB_STEP_SUMMARY"
    report_changes "$result_file" >> "$GITHUB_STEP_SUMMARY"
  done
  validate_pr_snapshot "$number" "$head" "$base"
  printf 'applied_head=%s\napplied_base=%s\n' "$head" "$base" >> "$GITHUB_OUTPUT"
}

merge_applied_pr() {
  local head="${EXPECTED_HEAD_SHA:?}" base="${EXPECTED_BASE_SHA:?}" number="${PR_NUMBER:?}" pr_json
  validate_pr_snapshot "$number" "$head" "$base"
  validate_pr_branch "$number" "${PR_BRANCH:?}"
  pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the PR before merge."
  jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
    || fail "The apply-request label was removed."
  jq -e '.mergeable_state == "clean" or .mergeable_state == "unstable"' <<< "$pr_json" >/dev/null \
    || fail "PR mergeability is blocked; no bypass will be attempted."
  require_successful_plan "$head" "$base" >/dev/null
  local result
  result=$(gh api -X PUT "repos/$GITHUB_REPOSITORY/pulls/$number/merge" \
    -f "sha=$head" -f merge_method=merge) || fail "GitHub rejected the merge."
  jq -e '.merged == true' <<< "$result" >/dev/null || fail "GitHub did not merge the PR."
  printf '## PR merged\nPR: #%s\n\nApplied commit: `%s`\n\nMerge commit: `%s`\n' \
    "$number" "$head" "$(jq -r '.sha' <<< "$result")" >> "$GITHUB_STEP_SUMMARY"
}

case "${1:-}" in
  prepare-apply) prepare_apply ;;
  check-config) check_apply_config ;;
  apply) run_apply ;;
  merge) merge_applied_pr ;;
  *) fail "Unknown apply command: ${1:-}" ;;
esac
