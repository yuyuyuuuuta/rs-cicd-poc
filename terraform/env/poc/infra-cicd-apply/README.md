# Terraform apply 基盤

検出された各Rootの共通CodePipeline、Apply用CodeBuild/IAM/OIDC、CodeConnections、ソース成果物S3を管理します。Plan用CodeBuildと保存Plan S3は[Plan基盤](../infra-cicd-plan/README.md)が管理します。

## Pipeline

| 項目           | 仕様                                                                          |
| -------------- | ----------------------------------------------------------------------------- |
| 本数・実行方式 | 検出Rootごとに1本。V2 / `QUEUED`                                              |
| ステージ       | Source → Plan → Apply                                                         |
| モード         | `ExecutionMode=plan                                                           | apply` を `VariableCheck` で照合し、対象外ステージは `SKIP` |
| Source         | CodeConnections、`COMMIT_ID` override による PR head SHA 固定                 |
| 自動変更検知   | `DetectChanges=false`。起動元は GitHub Actions                                |
| 実行権限       | Plan/Apply 用 CodeBuild ロールを分離。Apply は対象リソースと State キーに限定 |
| buildspec      | Terraform 管理のインライン設定。PR 内の buildspec には依存しない              |
| ログ           | CloudWatch Logs、保持期間14日。生診断は非公開 S3                              |

Pipeline名は`<TF_PIPELINE_NAME_PREFIX>-<root basename>-apply`で生成します。Pipeline変数は`ExecutionMode`、`PrCommitSha`、`PrNumber`、`BaseCommitSha`、`PlanRunId`、`PlanAttempt`、`ManifestVersion`です。CodeBuildがSourceのcommit SHAと要求SHAを照合します。RootとState keyはプロジェクトごとに固定です。

## Workflow と承認

| Workflow                                                                 | 契機・処理                                                                                                  |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------- |
| [terraform-plan.yml](../../../../.github/workflows/terraform-plan.yml)   | PR 作成・更新で静的解析、変更 Root 検出、Plan、Summary、commit status 記録                                  |
| [terraform-apply.yml](../../../../.github/workflows/terraform-apply.yml) | `apply-request` ラベルで PR/成功 Plan 確認、静的解析、再 Plan、Environment 承認、保存 Plan 適用、自動マージ |

Apply のジョブ依存関係は `inspect` → `static-checks` → `aws-replan` → `aws-apply` → `merge` です。Environment `terraform-apply` の Required reviewers は `aws-apply` だけに適用し、許可ブランチは `main` です。承認前の Plan は Summary に表示され、承認後に再作成されません。却下時は Apply/Merge しません。

GitHub Actionsの制御は[terraform_apply.sh](../../../../.github/scripts/terraform_apply.sh)と[terraform_plan.sh](../../../../.github/scripts/terraform_plan.sh)が担当し、Python版はworkflow内のコメントから切り替えられます。Rootは`terraform/env/poc/`直下の`.tf`を持つディレクトリから自動検出し、`infra-cicd-*`は除外します。共有module変更時は全対象Rootを選びます。`TF_PIPELINE_NAME_PREFIX`はGitHub repository variableとしてTerraformの`name_prefix`と一致させます。Actionsは信頼済みbase SHAの制御コードを使い、静的解析jobにはAWS Secretsを渡しません。forkとCI/CD基盤の変更は自動適用対象外です。

## 認証と設定

認証は GitHub OIDC を使用し、長期 AWS キーは保持しません。audience は `sts.amazonaws.com` です。

| ロール   | OIDC subject                                                                       |
| -------- | ---------------------------------------------------------------------------------- |
| Plan 用  | `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:pull_request`                |
| Apply 用 | `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:environment:terraform-apply` |

| GitHub Secret                   | 用途                                  |
| ------------------------------- | ------------------------------------- |
| `AWS_GITHUB_ACTIONS_ROLE_ARN`   | 承認後の Apply 用 OIDC ロール         |
| `AWS_GITHUB_PLAN_ROLE_ARN`      | PR Plan・承認前再 Plan 用 OIDC ロール |
| `AWS_ACCOUNT_ID` / `AWS_REGION` | AWS 接続先                            |
| `TF_STATE_BUCKET`               | State 保存先                          |
| `TF_TEST_BUCKET`                | 対象 S3                               |
| `TF_PLAN_ARTIFACT_BUCKET`       | 保存 Plan とメタデータの保存先        |

`TF_PIPELINE_NAME_PREFIX`はSecretではなくGitHub repository variableです。AWS Secretの設定例は[admin/github-secrets.local.json.example](../../../../admin/github-secrets.local.json.example)にあります。Plan CodeBuild名とPipeline名は共通`name_prefix`から自動生成します。

構築用 IAM と Pipeline/CodeBuild/Actions の実行用 IAM は分離しています。構築用の権限範囲は [admin/iam/terraform-cicd-policy.json.example](../../../../admin/iam/terraform-cicd-policy.json.example) に定義しています。

## 適用条件と制約

- PR Plan は同一 head/base の成功ステータスを確認し、保存 Plan の期限や保持期間には依存しません。Apply 前に作り直した Plan は PR/head/base/Root/Workflow run/attempt、S3 VersionId、SHA-256 と24時間の有効期限を照合し、承認後に `terraform apply <saved-plan>` で適用します。期限切れの場合は再 Plan と再承認が必要です。
- S3 の `approvals/` は Apply 用 OIDC ロールだけが書き込み可能です。CodeBuild は `consumed/` に排他作成し、Plan ごと・PR/head/Root ごとの適用試行を一度に制限します。失敗時も消費記録は残ります。
- Terraform の State ロックと古い Plan の拒否が有効です。承認後ジョブのリポジトリ単位 concurrency と Root ごとの Pipeline キューで競合を制御します。
- PR 更新・`apply-request` ラベル解除・PR 終了時は同じ PR の古い Workflow をキャンセルします。Apply ジョブが実行中の場合はキャンセルせず、新しい Apply はジョブ単位の concurrency で待機します。状態確認とキャンセルは原子的ではなく、開始済みの AWS 操作は自動停止・ロールバックされません。
- CodeBuild は公開 GitHub API で PR と Workflow を再確認します。非公開リポジトリや API 確認失敗時は適用しません。
- 全 Root 成功、最新 head/base、成功 Plan、`mergeable_state=clean` を満たす場合だけ、head SHA 指定の通常 merge API を使います。base の確認と merge API は原子的ではありません。
- 必須チェックはマージ前に完了する必要があり、Apply Workflow 全体の完了を必須にするとマージジョブとの循環が生じます。
- Summary は件数・操作種別・変更属性名と属性の追加・削除・変更・適用後確定を表示し、属性値・Outputs・生 Plan は非公開です。機密マーク付き属性とリソースの添字は表示しません。