# Terraform apply 基盤

Root ごとの共通 CodePipeline、Apply 用 CodeBuild/IAM/OIDC、CodeConnections、ソース成果物 S3 を管理します。対象 Root は `terraform/env/poc/ci-test` です。Plan 用 CodeBuild と保存 Plan S3 は [Plan 基盤](../infra-cicd-plan/README.md) が管理します。

## Pipeline

| 項目           | 仕様                                                                          |
| -------------- | ----------------------------------------------------------------------------- |
| 本数・実行方式 | 対象 Root ごとに1本。V2 / `QUEUED`                                            |
| ステージ       | Source → Plan → Apply                                                         |
| モード         | `ExecutionMode=plan                                                           | apply` を `VariableCheck` で照合し、対象外ステージは `SKIP` |
| Source         | CodeConnections、`COMMIT_ID` override による PR head SHA 固定                 |
| 自動変更検知   | `DetectChanges=false`。起動元は GitHub Actions                                |
| 実行権限       | Plan/Apply 用 CodeBuild ロールを分離。Apply は対象リソースと State キーに限定 |
| buildspec      | Terraform 管理のインライン設定。PR 内の buildspec には依存しない              |
| ログ           | CloudWatch Logs、保持期間14日。生診断は非公開 S3                              |

Pipeline 変数は `ExecutionMode`、`PrCommitSha`、`PrNumber`、`BaseCommitSha`、`PlanRunId`、`PlanAttempt`、`ManifestVersion` です。CodeBuild が Source の commit SHA と要求 SHA を照合します。Root と State キーはプロジェクトごとに固定です。

## Workflow と承認

| Workflow                                                                 | 契機・処理                                                                                                  |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| [terraform-plan.yml](../../../../.github/workflows/terraform-plan.yml)   | PR 作成・更新で静的解析、変更 Root 検出、Plan、Summary、commit status 記録                                  |
| [terraform-apply.yml](../../../../.github/workflows/terraform-apply.yml) | `apply-request` ラベルで PR/成功 Plan 確認、静的解析、再 Plan、Environment 承認、保存 Plan 適用、自動マージ |

Apply のジョブ依存関係は `inspect` → `static-checks` → `aws-replan` → `aws-apply` → `merge` です。Environment `terraform-apply` の Required reviewers は `aws-apply` だけに適用し、許可ブランチは `main` です。承認前の Plan は Summary に表示され、承認後に再作成されません。却下時は Apply/Merge しません。

共通制御は [.github/scripts/terraform_apply.py](../../../../.github/scripts/terraform_apply.py) です。`TARGETS` / `DEPENDENCIES` が Root と共通モジュールの依存関係を管理します。Actions は信頼済み base SHA の制御コードを使い、静的解析ジョブには AWS Secrets を渡しません。fork、未登録パス、CI/CD 基盤の変更は自動適用対象外です。

## 認証と設定

認証は GitHub OIDC を使用し、長期 AWS キーは保持しません。audience は `sts.amazonaws.com` です。

| ロール   | OIDC subject                                                                       |
| -------- | ---------------------------------------------------------------------------------- |
| Plan 用  | `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:pull_request`                |
| Apply 用 | `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:environment:terraform-apply` |

| GitHub Secret                   | 用途                                       |
| ------------------------------- | ------------------------------------------ |
| `AWS_GITHUB_ACTIONS_ROLE_ARN`   | 承認後の Apply 用 OIDC ロール              |
| `AWS_GITHUB_PLAN_ROLE_ARN`      | PR Plan・承認前再 Plan 用 OIDC ロール      |
| `AWS_ACCOUNT_ID` / `AWS_REGION` | AWS 接続先                                 |
| `TF_STATE_BUCKET`               | State 保存先                               |
| `TF_TEST_BUCKET`                | 対象 S3                                    |
| `TF_PIPELINE_NAMES`             | Root パスから共通 Pipeline 名への JSON map |
| `TF_PLAN_ARTIFACT_BUCKET`       | 保存 Plan とメタデータの保存先             |

設定例は [admin/github-secrets.local.json.example](../../../../admin/github-secrets.local.json.example) です。`plan_artifact_bucket_name` は Plan S3、`plan_project_names` は Plan 用 CodeBuild 名の対応表です。省略したプロジェクト名は共通 `name_prefix` から算出します。

構築用 IAM と Pipeline/CodeBuild/Actions の実行用 IAM は分離しています。構築用の権限範囲は [admin/iam/terraform-cicd-policy.json.example](../../../../admin/iam/terraform-cicd-policy.json.example) に定義しています。

## 適用条件と制約

- PR/head/base/Root/Workflow run/attempt、S3 VersionId、SHA-256、有効期限を保存 Plan と照合します。承認後は `terraform apply <saved-plan>` だけを実行します。
- S3 の `approvals/` は Apply 用 OIDC ロールだけが書き込み可能です。CodeBuild は `consumed/` に排他作成し、Plan ごと・PR/head/Root ごとの適用試行を一度に制限します。失敗時も消費記録は残ります。
- Terraform の State ロックと古い Plan の拒否が有効です。PR ごとの Workflow concurrency、承認後ジョブのリポジトリ単位 concurrency、Root ごとの Pipeline キューで競合を制御します。
- PR 更新・ラベル解除・PR 終了は古い Workflow をキャンセルしますが、開始済みの AWS 操作は自動停止・ロールバックされません。
- CodeBuild は公開 GitHub API で PR と Workflow を再確認します。非公開リポジトリや API 確認失敗時は適用しません。
- 全 Root 成功、最新 head/base、成功 Plan、`mergeable_state=clean` を満たす場合だけ、head SHA 指定の通常 merge API を使います。base の確認と merge API は原子的ではありません。
- 必須チェックはマージ前に完了する必要があり、Apply Workflow 全体の完了を必須にするとマージジョブとの循環が生じます。
- Summary は件数・操作種別・変更属性名・安全なエラー要約を表示し、属性値・Outputs・生 Plan は非公開です。