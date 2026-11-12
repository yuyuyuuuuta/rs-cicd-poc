#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_PARENT="terraform/env/poc"
readonly PLAN_STATUS_CONTEXT="Terraform plan"
readonly SHARED_PATH_PREFIXES=("modules/" "terraform/modules/" "buildspec/")

# -----------------------------------------------------------------------------
# pipeline_name_for_root: 対象ルートに対応するCodePipeline名を生成する。
# 引数: Terraformルートのパス。名前の末尾部分をPipeline名に使用する。
# 出力: TF_PIPELINE_NAME_PREFIX-ルート名-apply。プレフィックスが不正なら失敗する。
# -----------------------------------------------------------------------------
pipeline_name_for_root() {
  local root="$1" prefix="${TF_PIPELINE_NAME_PREFIX:?}"
  # プレフィックスを検証し、ルートに対応するPipeline名を出力する。
  [[ "$prefix" =~ ^[A-Za-z0-9.@_-]{1,60}$ ]] || fail "Invalid TF_PIPELINE_NAME_PREFIX repository variable."
  printf '%s-%s-apply\n' "$prefix" "${root##*/}"
}

# -----------------------------------------------------------------------------
# fail: エラーを報告し、スクリプトを終了する。
# 引数: 報告するエラーメッセージ。
# 出力: 標準エラー出力と、GITHUB_STEP_SUMMARYが設定されている場合はSummary。
# -----------------------------------------------------------------------------
fail() {
  # エラーメッセージを標準エラー出力とSummaryに記録して終了する。
  printf 'Workflow failed: %s\n' "$1" >&2
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '## Workflow failed\n%s\n' "$1" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 1
}

# -----------------------------------------------------------------------------
# discover_targets: 自動Planの候補となるTerraformルートを探す。
# 引数: リポジトリの作業ディレクトリ。ROOT_PARENT直下を検索する。
# 出力: .tfファイルを持つ対象ディレクトリの相対パスを1行ずつ返す。
# 除外: シンボリックリンク、不正なディレクトリ名、infra-cicd-で始まる名前。
# -----------------------------------------------------------------------------
discover_targets() {
  # 対象の親ディレクトリが存在しなければ空の結果を返す。
  local parent="$1/$ROOT_PARENT"
  [[ -d "$parent" ]] || return 0
  local directory name
  # 直下の通常ディレクトリから、条件を満たすTerraformルートを出力する。
  while IFS= read -r -d '' directory; do
    name=${directory##*/}
    [[ "$name" =~ ^[A-Za-z0-9_-]+$ && "$name" != infra-cicd-* ]] || continue
    if find "$directory" -maxdepth 1 -type f -name '*.tf' -print -quit | grep -q .; then
      printf '%s/%s\n' "$ROOT_PARENT" "$name"
    fi
  done < <(find "$parent" -mindepth 1 -maxdepth 1 -type d ! -type l -print0)
}

# -----------------------------------------------------------------------------
# validate_pr_snapshot: 処理対象のPRが期待した時点から変わっていないか確認する。
# 引数: PR番号、期待するhead SHA、期待するbase SHA。
# 検証: open、ドラフトでない、マージ可能、同一リポジトリのmain向けであること。
# 検証: head/baseが期待値と一致し、baseが現在のmainと一致すること。
# PR情報は最大3回取得し、条件を満たさない場合は処理を中断する。
# -----------------------------------------------------------------------------
validate_pr_snapshot() {
  local number="$1" expected_head="$2" expected_base="$3" attempt pr_json head base
  # GitHubからPR情報を取得し、head/baseとPRの状態を最大3回確認する。
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
  # PRのbaseが現在のmainと一致することを確認する。
  local current_main
  current_main=$(gh api "repos/$GITHUB_REPOSITORY/git/ref/heads/main" --jq '.object.sha') || fail "Could not read the current main SHA."
  [[ "$expected_base" == "$current_main" ]] || fail "The PR base is not the current main commit; retry after updating the PR."
}

# -----------------------------------------------------------------------------
# validate_pr_branch: ブランチから対象PRを一意に特定できるか確認する。
# 引数: PR番号、ソースブランチ名。
# 検証: 同一リポジトリのmain向けopen PRのうち、番号とブランチが一致するものが1件。
# -----------------------------------------------------------------------------
validate_pr_branch() {
  local number="$1" branch="$2" owner="${GITHUB_REPOSITORY%%/*}" matches
  # ブランチに対応する公開中のPRを取得し、対象PRだけが一致することを確認する。
  matches=$(gh api -X GET "repos/$GITHUB_REPOSITORY/pulls" -f state=open -f base=main -f "head=$owner:$branch") \
    || fail "Could not resolve the PR from its source branch."
  jq -e --arg number "$number" --arg branch "$branch" --arg repository "$GITHUB_REPOSITORY" \
    '[.[] | select((.number | tostring) == $number and .head.ref == $branch
      and .head.repo.full_name == $repository)] | length == 1' <<< "$matches" >/dev/null \
    || fail "The source branch does not identify exactly this open PR."
}

# -----------------------------------------------------------------------------
# require_successful_plan: 現在のhead/baseに対する自動Planの成功を確認する。
# 引数: PRのhead SHA、base SHA。
# 検証: 最新のTerraform planステータスが指定baseに対して成功していること。
# 検証: 参照先が同一リポジトリの想定Planワークフローで、対象headを実行済みであること。
# 出力: 検証済みワークフローの実行IDと試行番号をタブ区切りで返す。
# -----------------------------------------------------------------------------
require_successful_plan() {
  local head="$1" base="$2" statuses status run_url run_id run
  # 対象headの最新Planステータスが指定baseに対して成功していることを確認する。
  statuses=$(gh api "repos/$GITHUB_REPOSITORY/commits/$head/status") || fail "Could not read the PR plan status."
  status=$(jq -c --arg context "$PLAN_STATUS_CONTEXT" '[.statuses[] | select(.context == $context)] | sort_by(.created_at) | last // empty' <<< "$statuses")
  [[ -n "$status" ]] || fail "No automatic Terraform plan status exists for the current PR head."
  jq -e --arg description "Plan passed for base $base" '.state == "success" and .description == $description' \
    <<< "$status" >/dev/null || fail "A successful plan for this exact PR head and base is required."
  # ステータスが参照するワークフローの実行元、結果、headを確認する。
  run_url=$(jq -r '.target_url // empty' <<< "$status")
  [[ "$run_url" =~ ^https://github\.com/$GITHUB_REPOSITORY/actions/runs/([1-9][0-9]*)$ ]] || fail "The plan status has no trusted workflow run."
  run_id=${BASH_REMATCH[1]}
  run=$(gh api "repos/$GITHUB_REPOSITORY/actions/runs/$run_id") || fail "Could not verify the plan workflow run."
  jq -e --arg head "$head" --arg repository "$GITHUB_REPOSITORY" \
    '.conclusion == "success" and .event == "pull_request_target" and .path == ".github/workflows/terraform-plan.yml"
     and .head_sha == $head and .repository.full_name == $repository' <<< "$run" >/dev/null \
    || fail "The successful plan is not from the expected workflow and PR head."
  # 呼び出し元に実行IDと試行番号を返す。
  printf '%s\t%s\n' "$(jq -r '.id' <<< "$run")" "$(jq -r '.run_attempt' <<< "$run")"
}

# -----------------------------------------------------------------------------
# select_targets: PRの変更ファイルからPlan対象のTerraformルートを選択する。
# 引数: PR番号。変更前のファイル名も取得して対象判定に含める。
# 処理: 共有パスの変更は全検出ルートを選び、許可外パスやCI/CD基盤の変更は拒否する。
# 出力: 重複を除いて並べ替えたルートのJSON配列。対象がない場合は失敗する。
# -----------------------------------------------------------------------------
select_targets() {
  local number="$1"
  local -a changed_files available_targets
  # PRの変更ファイルと、作業ディレクトリ内で利用可能なルートを取得する。
  mapfile -t changed_files < <(gh api --paginate "repos/$GITHUB_REPOSITORY/pulls/$number/files?per_page=100" \
    --jq '.[] | .filename, .previous_filename // empty')
  ((${#changed_files[@]} <= 3000)) || fail "The PR exceeds the GitHub changed-files API limit."
  mapfile -t available_targets < <(discover_targets "$GITHUB_WORKSPACE")
  local -A selected=()
  local path root name all_targets=false
  # 変更パスから対象ルートを選び、共有パスの変更は全ルート対象とする。
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

  # 対象を重複なく並べ替え、JSON配列として出力する。
  local target
  if [[ "$all_targets" == true ]]; then
    for target in "${available_targets[@]}"; do selected["$target"]=1; done
  fi
  ((${#selected[@]} > 0)) || fail "The PR has no registered Terraform target changes."
  local -a roots=()
  mapfile -t roots < <(printf '%s\n' "${!selected[@]}" | sort -u)
  jq -cn '$ARGS.positional' --args "${roots[@]}"
}

# -----------------------------------------------------------------------------
# prepare_plan: 自動Planの実行に必要なPR情報と対象ルートを準備する。
# 入力: PR番号とイベント時点のhead/base SHA。pull_request_targetイベントのみ許可する。
# 処理: PRを検証して対象ルートとブランチを取得し、headのPlanステータスをpendingにする。
# 出力: PR番号、ブランチ、head/base、対象ルートをGITHUB_OUTPUTに設定する。
# -----------------------------------------------------------------------------
prepare_plan() {
  # PRイベントとhead/baseの状態を検証する。
  [[ "${GITHUB_EVENT_NAME:-}" == pull_request_target ]] || fail "Automatic planning requires a pull_request_target event."
  local number="${PR_NUMBER:?}" expected_head="${EVENT_PR_HEAD_SHA:?}" expected_base="${EVENT_PR_BASE_SHA:?}"
  validate_pr_snapshot "$number" "$expected_head" "$expected_base"

  # 対象ルートとブランチを取得し、後続ジョブ用の出力に設定する。
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
  # コミットのPlanステータスを実行中に更新する。
  gh api -X POST "repos/$GITHUB_REPOSITORY/statuses/$expected_head" \
    -f state=pending -f context="$PLAN_STATUS_CONTEXT" -f "description=Plan is running for base $expected_base" >/dev/null \
    || fail "Could not mark the PR commit status as pending."
  # PR番号、コミット、対象の概要をSummaryに出力する。
  {
    printf '## Terraform plan\nPR: #%s\n\nCommit: `%s`\n\nTargets: %s\n' \
      "$number" "$expected_head" "$(IFS=', '; echo "${roots[*]}")"
  } >> "$GITHUB_STEP_SUMMARY"
}

# -----------------------------------------------------------------------------
# validate_targets: PR候補のTerraformルートをAWS接続なしで静的に検証する。
# 入力: CANDIDATE_DIRとTARGET_ROOTSのJSON配列。
# 検証: 対象パスの形式、ディレクトリの存在、解決先、およびシンボリックリンクの有無。
# 処理: 各ルートでfmt -check、backend無効のinit、validateを順に実行する。
# -----------------------------------------------------------------------------
validate_targets() {
  # 対象ルートを読み込み、空でないことを確認する。
  local candidate="${CANDIDATE_DIR:?}" root directory resolved
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
  for root in "${roots[@]}"; do
    # ルートの形式と実体を検証し、シンボリックリンクを拒否する。
    [[ "$root" =~ ^terraform/env/poc/[A-Za-z0-9_-]+$ && "$root" != terraform/env/poc/infra-cicd-* ]] || fail "Invalid Terraform target root: $root"
    directory="$candidate/$root"
    [[ -d "$directory" && ! -L "$directory" ]] || fail "Terraform target root is missing or is a symlink: $root"
    resolved=$(realpath "$directory")
    [[ "$resolved" == "$directory" ]] || fail "Terraform target root resolves outside the candidate tree: $root"
    if find "$directory" -type l -print -quit | grep -q .; then fail "Symlinks are not allowed in a Terraform target: $root"; fi
    # backendに接続せず、書式とTerraform構成を検証する。
    terraform -chdir="$directory" fmt -check -recursive
    terraform -chdir="$directory" init -backend=false -input=false -lockfile=readonly
    terraform -chdir="$directory" validate -no-color
  done
}

# -----------------------------------------------------------------------------
# check_plan_config: Plan用のAWS接続設定とリソース名の形式を検証する。
# 入力: AWSアカウント、リージョン、PlanロールARN、Pipelineプレフィックス、各バケット名。
# 検証: 必須値、各値の形式、およびロールARNのアカウントと指定アカウントの一致。
# -----------------------------------------------------------------------------
check_plan_config() {
  local account="${AWS_ACCOUNT_ID:?}" region="${AWS_REGION:?}" role="${AWS_GITHUB_PLAN_ROLE_ARN:?}"
  local prefix="${TF_PIPELINE_NAME_PREFIX:?}" bucket
  # AWS接続先、Planロール、Pipeline名の形式と対応を検証する。
  [[ "$account" =~ ^[0-9]{12}$ ]] || fail "Invalid AWS_ACCOUNT_ID secret."
  [[ "$region" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || fail "Invalid AWS_REGION secret."
  [[ "$role" =~ ^arn:aws:iam::$account:role/[A-Za-z0-9_+=,.@/-]+$ ]] || fail "The plan role ARN does not match AWS_ACCOUNT_ID."
  [[ "$prefix" =~ ^[A-Za-z0-9.@_-]{1,60}$ ]] || fail "Invalid TF_PIPELINE_NAME_PREFIX repository variable."
  # State、テスト、Plan保存用バケット名の形式を検証する。
  for bucket in "${TF_STATE_BUCKET:?}" "${TF_TEST_BUCKET:?}" "${TF_PLAN_ARTIFACT_BUCKET:?}"; do
    [[ "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "Invalid bucket name secret."
  done
}

# -----------------------------------------------------------------------------
# verify_plan_manifest: 保存Planのマニフェストが今回のPlan結果と一致するか確認する。
# 引数: ルート、結果JSON、保存先ファイル2つ、ワークフロー実行ID、試行番号。
# 処理: S3からマニフェストを取得し、PR/head/base/ルート/実行の紐付けを確認する。
# 検証: 結果と一致するS3バージョンとSHA-256、Planのバージョン、24時間以内の有効期限。
# 出力: マニフェストのバージョン、SHA-256、Pipeline実行IDをJSONで返す。
# -----------------------------------------------------------------------------
verify_plan_manifest() {
  local root="$1" result_file="$2" manifest_file="$3" response_file="$4" run_id="$5" attempt="$6"
  local root_hash prefix manifest_version now
  # 対象ルートと実行情報からS3の保存先を組み立て、マニフェストを取得する。
  root_hash=$(printf '%s' "$root" | sha256sum | cut -c1-16)
  prefix="plans/$GITHUB_REPOSITORY/pr-$PR_NUMBER/$EXPECTED_HEAD_SHA/$root_hash/$run_id/$attempt"
  aws s3api get-object --bucket "$TF_PLAN_ARTIFACT_BUCKET" --key "$prefix/manifest.json" "$manifest_file" > "$response_file"
  manifest_version=$(jq -r '.VersionId // empty' "$response_file")
  [[ -n "$manifest_version" ]] || fail "The saved plan manifest has no S3 version."
  now=$(date +%s)
  # PR・実行情報、バージョン、SHA-256、有効期限がPlan結果と一致することを確認する。
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
  # 後続の承認・Applyに渡す保存Planの参照を出力する。
  jq -cn --arg version "$manifest_version" --arg sha "$(jq -r '.sha256' "$manifest_file")" \
    --arg execution "$EXECUTION_ID" '{manifest_version:$version,sha256:$sha,execution_id:$execution}'
}

# -----------------------------------------------------------------------------
# report_plan_result: 検証済みCodeBuild Plan結果をActionsのSummaryに表示する。
# 引数: Terraformルート、結果JSONファイル、CodeBuildのステータス。
# 検証: 結果が今回のPR/head/base/実行に対応し、PlanとCodeBuildが成功していること。
# 出力: 追加・変更・削除の件数、Pipeline実行ID、リソースごとの変更情報。
# -----------------------------------------------------------------------------
report_plan_result() {
  local root="$1" result_file="$2" build_status="$3"
  # CodeBuildの結果が対象PRと実行に対応し、成功していることを確認する。
  jq -e --arg root "$root" --arg repository "$GITHUB_REPOSITORY" --arg pr "$PR_NUMBER" \
    --arg head "$EXPECTED_HEAD_SHA" --arg base "$EXPECTED_BASE_SHA" --arg run "$GITHUB_RUN_ID" \
    --arg attempt "$GITHUB_RUN_ATTEMPT" \
    '.root == $root and .phase == "plan" and .status == "SUCCEEDED" and .repository == $repository
     and .pr == $pr and .head == $head and .base == $base and .run_id == $run and .attempt == $attempt' \
    "$result_file" >/dev/null || fail "CodeBuild returned an unverified plan result for $root."
  [[ "$build_status" == SUCCEEDED ]] || fail "Terraform CodeBuild failed for $root."
  # 変更件数を読み取り、実行IDとリソースの変更内容をSummaryに出力する。
  local add change destroy
  add=$(jq -r '.counts.add // 0' "$result_file")
  change=$(jq -r '.counts.change // 0' "$result_file")
  destroy=$(jq -r '.counts.destroy // 0' "$result_file")
  {
    printf '### Terraform plan: %s\n\n' "$root"
    printf '| Added | Changed | Destroyed |\n| ---: | ---: | ---: |\n| %s | %s | %s |\n\n' "$add" "$change" "$destroy"
    printf 'Execution: `%s`\n\n' "$EXECUTION_ID"
    report_changes "$result_file"
  } >> "$GITHUB_STEP_SUMMARY"
}

# -----------------------------------------------------------------------------
# report_changes: 構造化されたPlan/Apply結果から表示用の変更情報を整形する。
# 引数: CodeBuildの結果JSONファイル。
# 出力: リソースアドレス、操作、属性名、属性ごとの変更種別をJSONとして出力する。
# 制限: インスタンスキーを[*]に置き換え、属性値などの他の項目は出力しない。
# -----------------------------------------------------------------------------
report_changes() {
  local result_file="$1"
  # 表示する項目を絞り、インスタンスキーを伏せたJSONを出力する。
  printf '```json\n'
  jq '[.changes[]? | {address: (.address | gsub("\\[(\"([^\"\\\\]|\\\\.)*\"|[0-9]+)\\]"; "[*]")), actions, attributes, attribute_changes: [.attribute_changes[]? | {name, change}]}]' "$result_file"
  printf '```\n\n'
}

# -----------------------------------------------------------------------------
# run_plan: 対象PRの各TerraformルートでPlanを実行し、保存Planの参照をまとめる。
# 入力: PR番号、ブランチ、head/base SHA、対象ルート、GitHub Actions実行情報。
# 処理: PRを検証し、必要なら事前のPR Plan成功を確認して、ルートごとにPipelineを起動する。
# 検証: Pipelineの成功とソースSHA、PR状態、CodeBuild結果、S3マニフェストを確認する。
# 出力: Plan結果をSummaryに記録し、head/baseと各保存Planの参照をGITHUB_OUTPUTに設定する。
# -----------------------------------------------------------------------------
run_plan() {
  local number="${PR_NUMBER:?}" head="${EXPECTED_HEAD_SHA:?}" base="${EXPECTED_BASE_SHA:?}"
  # PRと対象ルートを確認し、必要な場合は事前のPR Plan成功を検証する。
  validate_pr_snapshot "$number" "$head" "$base"
  validate_pr_branch "$number" "${PR_BRANCH:?}"
  mapfile -t roots < <(jq -r '.[]' <<< "${TARGET_ROOTS:?}")
  ((${#roots[@]} > 0)) || fail "No Terraform target roots were provided."
  if [[ "${REQUIRE_PR_PLAN:-false}" == true ]]; then
    require_successful_plan "$head" "$base" >/dev/null
  fi
  local root pipeline root_hash request_token variables_json execution attempt status execution_json revision
  local action_json build_id build_json log_group log_stream result_record result_file manifest_file manifest_response plans_json
  plans_json='{}'
  for root in "${roots[@]}"; do
    # ルートごとのPipelineを、対象コミットとPRの実行情報を指定して起動する。
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

    # Pipelineの完了を待ち、ソースのSHAと現在のPR状態を確認する。
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

    # PipelineのPlanアクションからCodeBuildとログの保存先を特定する。
    action_json=$(aws codepipeline list-action-executions --pipeline-name "$pipeline" \
      --filter "pipelineExecutionId=$execution" --output json)
    build_id=$(jq -r '[.actionExecutionDetails[] | select(.actionName == "TerraformPlan" and .stageName == "Plan")
      | .output.executionResult.externalExecutionId // empty] | last // empty' <<< "$action_json")
    [[ -n "$build_id" ]] || fail "TerraformPlan CodeBuild execution was not found for $root."
    build_json=$(aws codebuild batch-get-builds --ids "$build_id" --output json)
    log_group=$(jq -r '.builds[0].logs.groupName // empty' <<< "$build_json")
    log_stream=$(jq -r '.builds[0].logs.streamName // empty' <<< "$build_json")
    [[ -n "$log_group" && -n "$log_stream" ]] || fail "CodeBuild log stream is unavailable for $root."

    # CloudWatch Logsをページ単位で読み、構造化されたPlan結果を取り出す。
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

    # 結果と保存Planを検証し、対象ルートごとの参照情報をまとめる。
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
  # 最後にPRを再確認し、PlanのSHAと保存Plan参照を後続ジョブへ出力する。
  validate_pr_snapshot "$number" "$head" "$base"
  printf 'planned_head=%s\nplanned_base=%s\nplans=%s\n' "$head" "$base" "$plans_json" >> "$GITHUB_OUTPUT"
}

# -----------------------------------------------------------------------------
# finalize_plan_status: 自動Planの各ジョブ結果をコミットステータスに反映する。
# 入力: 対象head/base SHA、事前確認・静的チェック・Planの結果、ワークフローURL。
# 処理: SHAが有効な場合、全ジョブ成功ならsuccess、それ以外ならfailureをGitHubに登録する。
# 出力: 最終ステータスをActionsのSummaryに記録する。SHAが不正な場合は更新しない。
# -----------------------------------------------------------------------------
finalize_plan_status() {
  local head="${PLAN_HEAD_SHA:-}" base="${PLAN_BASE_SHA:-}" state description
  # SHAと各ジョブの結果を確認し、最終ステータスを決定する。
  [[ "$head" =~ ^[0-9a-f]{40}$ && "$base" =~ ^[0-9a-f]{40}$ ]] || return 0
  if [[ "${PREFLIGHT_RESULT:-}" == success && "${STATIC_CHECK_RESULT:-}" == success && "${PLAN_RESULT:-}" == success ]]; then
    state=success
    description="Plan passed for base $base"
  else
    state=failure
    description="Automatic Terraform plan failed; review the workflow run"
  fi
  # GitHubのコミットステータスを更新し、結果をSummaryに記録する。
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