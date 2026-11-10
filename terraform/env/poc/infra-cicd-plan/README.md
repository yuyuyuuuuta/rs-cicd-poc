# Terraform plan 基盤

`ci-test` の Plan 用 CodeBuild、IAM/OIDC ロール、保存 Plan 用 S3 を管理する Root です。State と実行ロールは Apply 基盤から分離し、Pipeline は [Apply 基盤](../infra-cicd-apply/README.md) の共通 Pipeline を使用します。

## 構成

- 共通 Pipeline の Plan ステージが CodeBuild を起動します。Plan 専用 Pipeline は持ちません。
- CodeConnections の `COMMIT_ID` override で PR head SHA を固定し、CodeBuild が取得した SHA と照合します。
- Plan 用ロールは対象 AWS リソースと State 本体を読み取り、ロックファイル・Plan 成果物・診断・ログだけを書き込みます。
- buildspec は Terraform 管理のインライン設定です。実行用 Python は Apply 基盤の `buildspec/terraform_runner.py` を共用します。
- PR Plan と Apply 承認前の再 Plan は、同じ CodeBuild を別実行として使用します。

## 認証と入力

Plan 用 OIDC subject は `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:pull_request`、audience は `sts.amazonaws.com` です。`pull_request_target` が使用する base branch の ref と OIDC subject は同一ではありません。

| 設定                          | 用途                                                                 |
| ----------------------------- | -------------------------------------------------------------------- |
| `AWS_GITHUB_PLAN_ROLE_ARN`    | Actions が使用する Plan 用 OIDC ロール                               |
| `TF_PLAN_ARTIFACT_BUCKET`     | 保存 Plan とメタデータの保存先                                       |
| `TF_PIPELINE_NAMES`           | Root パスから共通 Pipeline 名への対応表                              |
| `source_artifact_bucket_name` | ソース取得用 S3 の読み取り許可対象                                   |
| `shared_pipeline_names`       | 共通 Pipeline 名の明示的な対応表。省略時は共通プレフィックスから算出 |

## Plan 成果物

Plan 本体とメタデータは、暗号化・バージョニング・公開ブロック・TLS 必須ポリシーを持つ S3 に保存します。メタデータは PR/head/base/Root/Workflow run/attempt、Plan の S3 VersionId、SHA-256、Terraform バージョン、有効期限を保持します。

Plan の有効期間は24時間です。S3 の `plans/` と `approvals/` は現行版・非現行版とも2日、`diagnostics/` は7日で期限削除し、`consumed/` は保持します。S3 の削除処理とは別に実行時にも期限を検証します。

PR head に `Terraform plan` status を記録し、description に base SHA を含めます。Summary は件数、操作種別、変更属性名のみを表示し、生 Plan・属性値・Outputs は表示しません。生診断は非公開 S3 に保存します。