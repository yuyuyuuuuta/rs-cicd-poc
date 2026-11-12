#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/terraform_plan.sh"

APPLY_TEMP_DIRECTORY=""
# -----------------------------------------------------------------------------
# cleanup_apply_temp_directory: Apply処理で作成した一時ディレクトリを削除する。
# 入力: APPLY_TEMP_DIRECTORY。未設定または空の場合は何もしない。
# 実行契機: スクリプト終了時にEXITトラップから呼び出される。
# -----------------------------------------------------------------------------
cleanup_apply_temp_directory() {
  # 一時ディレクトリが設定されている場合だけ削除する。
  [[ -z "$APPLY_TEMP_DIRECTORY" ]] || rm -rf "$APPLY_TEMP_DIRECTORY"
}
trap cleanup_apply_temp_directory EXIT

# -----------------------------------------------------------------------------
# validate_apply_label_event: Applyを起動したイベントと実行者の権限を確認する。
# 入力: GitHubイベントのJSON、イベント名、参照、イベント送信者と実行者。
# 検証: main向けpull_request_targetのapply-requestラベル追加イベントであること。
# 検証: ラベル送信者とワークフロー実行者がリポジトリへの書き込み権限を持つこと。
# -----------------------------------------------------------------------------
validate_apply_label_event() {
  local event_file="${GITHUB_EVENT_PATH:?}" event sender actor permission
  # イベントの種別、対象ブランチ、追加されたラベルを確認する。
  [[ "${GITHUB_EVENT_NAME:-}" == pull_request_target && "${GITHUB_REF:-}" == refs/heads/main ]] \
    || fail "Apply must start from a pull_request_target event targeting main."
  event=$(jq -c . "$event_file") || fail "Could not parse the GitHub event payload."
  jq -e '.action == "labeled" and .label.name == "apply-request"' <<< "$event" >/dev/null \
    || fail "Apply must start when the apply-request label is added."
  sender=$(jq -r '.sender.login // empty' <<< "$event")
  actor="${GITHUB_TRIGGERING_ACTOR:-$sender}"
  [[ -n "$sender" && -n "$actor" ]] || fail "Could not identify the label sender and workflow actor."
  # 送信者と実行者それぞれのリポジトリ権限をGitHub APIで確認する。
  for actor in "$sender" "${GITHUB_TRIGGERING_ACTOR:-$sender}"; do
    permission=$(gh api "repos/$GITHUB_REPOSITORY/collaborators/$actor/permission" --jq '.permission') \
      || fail "Could not verify repository permission for $actor."
    [[ "$permission" == write || "$permission" == maintain || "$permission" == admin ]] \
      || fail "The label sender and workflow actor must have repository write permission."
  done
}

# -----------------------------------------------------------------------------
# prepare_apply: Applyワークフローの対象PRとTerraformルートを決定する。
# 入力: ラベル追加イベントに含まれるPR番号、head SHA、base SHA。
# 検証: PRの状態、ラベル、同じhead/baseのPlan成功、ブランチとPRの対応。
# 出力: PR番号、ブランチ、head/base、対象ルートを後続ジョブとSummaryに渡す。
# -----------------------------------------------------------------------------
prepare_apply() {
  # イベントの送信者と実行者を検証し、イベント時点のPR情報を取得する。
  validate_apply_label_event
  local event number head base pr_json roots plan_run
  event=$(jq -c . "${GITHUB_EVENT_PATH:?}")
  number=$(jq -r '.pull_request.number' <<< "$event")
  head=$(jq -r '.pull_request.head.sha' <<< "$event")
  base=$(jq -r '.pull_request.base.sha' <<< "$event")
  # 現在のPRとラベル、成功済みPlan、対象ルートを確認する。
  validate_pr_snapshot "$number" "$head" "$base"
  pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the current PR."
  jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
    || fail "The apply-request label was removed."
  plan_run=$(require_successful_plan "$head" "$base")
  roots=$(select_targets "$number")
  local branch
  branch=$(jq -r '.head.ref' <<< "$pr_json")
  validate_pr_branch "$number" "$branch"
  # 対象PRとルートを後続ジョブに渡す。
  {
    printf 'pr_number=%s\n' "$number"
    printf 'pr_branch=%s\n' "$branch"
    printf 'head_sha=%s\n' "$head"
    printf 'base_sha=%s\n' "$base"
    printf 'targets=%s\n' "$roots"
  } >> "$GITHUB_OUTPUT"
  # 事前確認したPRとPlanの概要をSummaryに出力する。
  {
    printf '## Terraform apply preflight\nPR: #%s\n\nBranch: `%s`\n\nCommit: `%s`\n\nTargets: %s\n\nPlan run: `%s`\n' \
      "$number" "$branch" "$head" "$(jq -r 'join(", ")' <<< "$roots")" "${plan_run%%$'\t'*}"
  } >> "$GITHUB_STEP_SUMMARY"
}

# -----------------------------------------------------------------------------
# check_apply_config: Apply用のAWS接続設定と対象ルートを検証する。
# 入力: AWSアカウント、リージョン、ApplyロールARN、Pipelineプレフィックス、各バケット名。
# 検証: 各設定の形式、ロールARNとアカウントの一致、および対象ルートが空でないこと。
# -----------------------------------------------------------------------------
check_apply_config() {
  local account="${AWS_ACCOUNT_ID:?}" region="${AWS_REGION:?}" role="${AWS_GITHUB_ACTIONS_ROLE_ARN:?}"
  local prefix="${TF_PIPELINE_NAME_PREFIX:?}" bucket
  # アカウント、リージョン、Applyロール、Pipelineプレフィックスの形式を確認する。
  [[ "$account" =~ ^[0-9]{12}$ ]] || fail "Invalid AWS_ACCOUNT_ID secret."
  [[ "$region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || fail "Invalid AWS_REGION secret."
  [[ "$role" =~ ^arn:aws:iam::$account:role/[A-Za-z0-9_+=,.@/-]+$ ]] || fail "The apply role ARN does not match AWS_ACCOUNT_ID."
  [[ "$prefix" =~ ^[A-Za-z0-9.@_-]{1,60}$ ]] || fail "Invalid TF_PIPELINE_NAME_PREFIX repository variable."
  # State、テスト、保存Planのバケット名と対象ルートを確認する。
  for bucket in "${TF_STATE_BUCKET:?}" "${TF_TEST_BUCKET:?}" "${TF_PLAN_ARTIFACT_BUCKET:?}"; do
    [[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "Invalid bucket name secret."
  done
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
}

# -----------------------------------------------------------------------------
# verify_approved_manifest: 承認対象の保存Planが今回のApplyに対応するか検証する。
# 引数: Terraformルート、保存Plan参照、ワークフロー実行ID、試行番号、一時ディレクトリ。
# 検証: S3バージョン、SHA-256、PR/head/base/ルート/実行の紐付けと24時間以内の有効期限。
# 出力: 承認記録に保存する紐付け情報、マニフェストのバージョンとSHA-256をJSONで返す。
# -----------------------------------------------------------------------------
verify_approved_manifest() {
  local root="$1" saved="$2" run_id="$3" attempt="$4" directory="$5"
  local version sha root_hash prefix response manifest_file
  # 保存Plan参照からマニフェストのバージョンとSHA-256を取得する。
  version=$(jq -r '.manifest_version // empty' <<< "$saved")
  sha=$(jq -r '.sha256 // empty' <<< "$saved")
  [[ -n "$version" && "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "Saved plan reference is missing its S3 version or digest for $root."
  # 指定バージョンのマニフェストをS3から取得する。
  root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
  prefix="plans/$GITHUB_REPOSITORY/pr-$PR_NUMBER/$EXPECTED_HEAD_SHA/$root_hash/$run_id/$attempt"
  manifest_file="$directory/manifest-$root_hash.json"
  response="$directory/manifest-response-$root_hash.json"
  aws s3api get-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" --key "$prefix/manifest.json" \
    --version-id "$version" "$manifest_file" > "$response"
  # マニフェストの実行情報、SHA-256、有効期限、S3バージョンを検証する。
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
  # 検証済みの紐付け情報を承認記録用のJSONとして返す。
  jq -cn --argjson binding "$(jq -cn --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" \
    --arg head "$EXPECTED_HEAD_SHA" --arg base "$EXPECTED_BASE_SHA" --arg root "$root" \
    --arg run "$run_id" --arg attempt "$attempt" \
    '{repository:$repository,pr:$pr,head:$head,base:$base,root:$root,run_id:$run,attempt:$attempt}')" \
    --arg version "$version" --arg sha "$sha" '{binding:$binding,manifest_version:$version,sha256:$sha}'
}

# -----------------------------------------------------------------------------
# wait_for_pipeline: 指定したCodePipeline実行の完了を待つ。
# 引数: Pipeline名、Pipeline実行ID。
# 処理: 実行状態を最大160回、15秒間隔で確認し、成功以外は処理を中断する。
# 出力: 成功したPipeline実行のJSONを返す。
# -----------------------------------------------------------------------------
wait_for_pipeline() {
  local pipeline="$1" execution="$2" attempt status execution_json
  status=InProgress
  # 実行中または停止処理中は状態を再取得する。
  for attempt in $(seq 1 160); do
    execution_json=$(aws codepipeline get-pipeline-execution --pipeline-name "$pipeline" \
      --pipeline-execution-id "$execution" --output json)
    status=$(jq -r '.pipelineExecution.status' <<< "$execution_json")
    [[ "$status" == InProgress || "$status" == Stopping ]] || break
    sleep 15
  done
  # 成功を確認し、Pipeline実行情報を呼び出し元に返す。
  [[ "$status" == Succeeded ]] || fail "CodePipeline ended with status $status."
  printf '%s' "$execution_json"
}

# -----------------------------------------------------------------------------
# read_build_result: Pipeline実行に対応するCodeBuildの構造化結果を取得する。
# 引数: Pipeline名、実行ID、アクション名、ステージ名、結果JSONの保存先。
# 処理: CodeBuild実行とCloudWatch Logsを特定し、TF_RESULT_JSONをログから取り出す。
# 出力: 結果JSONを指定ファイルに保存し、ファイル名とビルド状態をJSONで返す。
# -----------------------------------------------------------------------------
read_build_result() {
  local pipeline="$1" execution="$2" action_name="$3" stage_name="$4" result_file="$5"
  local action_json build_id build_json log_group log_stream next_token='' next_forward_token page records attempt
  # 指定したPipelineアクションのCodeBuild実行とログ保存先を特定する。
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
  # CloudWatch Logsをページ単位で読み、構造化された結果を探す。
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
      # 結果をファイルに保存し、保存先とCodeBuildの状態を返す。
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

# -----------------------------------------------------------------------------
# run_apply: 承認済みPlanを対象ルートごとに適用し、結果を確認する。
# 入力: PR番号、ブランチ、head/base SHA、対象ルート、承認前に保存されたPlan参照。
# 検証: PRとラベル、成功済みPR Plan、保存Planの対象範囲とマニフェストを確認する。
# 処理: ルートごとに承認記録をS3へ保存し、Apply Pipelineを起動して完了と結果を検証する。
# 出力: 実行情報と変更内容をSummaryに記録し、適用済みhead/baseを後続ジョブに渡す。
# -----------------------------------------------------------------------------
run_apply() {
  local number="${PR_NUMBER:?}" head="${EXPECTED_HEAD_SHA:?}" base="${EXPECTED_BASE_SHA:?}"
  # イベント、PR、ブランチ、ラベル、および事前Planの成功を確認する。
  validate_apply_label_event
  validate_pr_snapshot "$number" "$head" "$base"
  validate_pr_branch "$number" "${PR_BRANCH:?}"
  local pr_json
  pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the current PR."
  jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
    || fail "The apply-request label was removed."
  require_successful_plan "$head" "$base" >/dev/null
  # 保存されたPlanが選択された全ルートと一致することを確認する。
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
    # 各ルートの開始前にPRとラベルを再確認し、承認Planを検証する。
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
    # Pipelineに渡すPR情報と保存Planのバージョンを設定する。
    variables_json=$(jq -cn --arg head "$head" --arg base "$base" --arg pr "$number" \
      --arg run "$GITHUB_RUN_ID" --arg attempt "$GITHUB_RUN_ATTEMPT" \
      --arg version "$(jq -r '.manifest_version' <<< "$saved")" \
      '[{name:"PrCommitSha",value:$head},{name:"ExecutionMode",value:"apply"},{name:"PrNumber",value:$pr},
        {name:"BaseCommitSha",value:$base},{name:"PlanRunId",value:$run},{name:"PlanAttempt",value:$attempt},
        {name:"ManifestVersion",value:$version}]')
    # 検証済みPlanの承認記録を排他的に保存し、Apply Pipelineを起動する。
    aws s3api put-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" \
      --key "approvals/plans/$GITHUB_REPOSITORY/pr-$number/$head/$(printf '%s' "$root" | sha256sum | cut -c1-16)/$GITHUB_RUN_ID/$GITHUB_RUN_ATTEMPT.json" \
      --body "$approval_file" --server-side-encryption AES256 --if-none-match '*' >/dev/null
    execution=$(aws codepipeline start-pipeline-execution --name "$pipeline" --client-request-token "$request_token" \
      --source-revisions "actionName=Source,revisionType=COMMIT_ID,revisionValue=$head" --variables "$variables_json" \
      --query pipelineExecutionId --output text)
    # 起動したApplyのPR、コミット、ルート、実行IDをSummaryに出力する。
    printf '## AWS Terraform apply\nPR: #%s\n\nCommit: `%s`\n\nRoot: `%s`\n\nExecution: `%s`\n' \
      "$number" "$head" "$root" "$execution" >> "$GITHUB_STEP_SUMMARY"
    # Pipelineの完了を待ち、ソースSHAとPRの状態を再確認する。
    execution_json=$(wait_for_pipeline "$pipeline" "$execution")
    revision=$(jq -r '.pipelineExecution.artifactRevisions[]? | select(.name == "SourceOutput") | .revisionId' <<< "$execution_json" | tail -n 1)
    [[ "$revision" == "$head" ]] || fail "CodePipeline source revision does not match the requested PR head for $root."
    validate_pr_snapshot "$number" "$head" "$base"
    # CodeBuildの結果が今回のPRと実行に一致し、正常終了したことを確認する。
    result_file="$APPLY_TEMP_DIRECTORY/apply-$root_hash.json"
    result_info=$(read_build_result "$pipeline" "$execution" TerraformApply Apply "$result_file")
    build_status=$(jq -r '.build_status' <<< "$result_info")
    jq -e --arg root "$root" --arg repository "$GITHUB_REPOSITORY" --arg pr "$number" \
      --arg head "$head" --arg base "$base" --arg run "$GITHUB_RUN_ID" --arg attempt "$GITHUB_RUN_ATTEMPT" \
      '.root == $root and .phase == "apply" and .status == "SUCCEEDED" and .repository == $repository
       and .pr == $pr and .head == $head and .base == $base and .run_id == $run and .attempt == $attempt' \
      "$result_file" >/dev/null || fail "CodeBuild returned an unverified apply result for $root."
    [[ "$build_status" == SUCCEEDED ]] || fail "Terraform CodeBuild failed for $root."
    # Applyの変更件数とリソースごとの変更情報をSummaryに出力する。
    jq -r --arg root "$root" --arg execution "$execution" \
      '"### Apply result: " + $root + "\n\n| Added | Changed | Destroyed |\n| ---: | ---: | ---: |\n| " + (.counts.add|tostring) + " | " + (.counts.change|tostring) + " | " + (.counts.destroy|tostring) + " |\n\nExecution: `" + $execution + "`\n"' \
      "$result_file" >> "$GITHUB_STEP_SUMMARY"
    report_changes "$result_file" >> "$GITHUB_STEP_SUMMARY"
  done
  # 全ルートの適用後にPRを再確認し、適用済みSHAを後続ジョブに渡す。
  validate_pr_snapshot "$number" "$head" "$base"
  printf 'applied_head=%s\napplied_base=%s\n' "$head" "$base" >> "$GITHUB_OUTPUT"
}

# -----------------------------------------------------------------------------
# merge_applied_pr: Applyが成功したPRの指定headを通常のGitHub APIでマージする。
# 入力: PR番号、ブランチ、適用済みhead SHAとbase SHA。
# 検証: PRとブランチ、ラベル、マージ可能状態、同じhead/baseのPlan成功を再確認する。
# 出力: マージされたPR、適用済みコミット、マージコミットをSummaryに記録する。
# -----------------------------------------------------------------------------
merge_applied_pr() {
  local head="${EXPECTED_HEAD_SHA:?}" base="${EXPECTED_BASE_SHA:?}" number="${PR_NUMBER:?}" pr_json
  # マージ前にPR、ブランチ、ラベル、マージ可能状態、Plan成功を確認する。
  validate_pr_snapshot "$number" "$head" "$base"
  validate_pr_branch "$number" "${PR_BRANCH:?}"
  pr_json=$(gh api "repos/$GITHUB_REPOSITORY/pulls/$number") || fail "Could not read the PR before merge."
  jq -e '[.labels[].name] | index("apply-request") != null' <<< "$pr_json" >/dev/null \
    || fail "The apply-request label was removed."
  jq -e '.mergeable_state == "clean" or .mergeable_state == "unstable"' <<< "$pr_json" >/dev/null \
    || fail "PR mergeability is blocked; no bypass will be attempted."
  require_successful_plan "$head" "$base" >/dev/null
  # head SHAを指定してマージを要求し、GitHubがマージしたことを確認する。
  local result
  result=$(gh api -X PUT "repos/$GITHUB_REPOSITORY/pulls/$number/merge" \
    -f "sha=$head" -f merge_method=merge) || fail "GitHub rejected the merge."
  jq -e '.merged == true' <<< "$result" >/dev/null || fail "GitHub did not merge the PR."
  # マージ結果をSummaryに記録する。
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
