# Terraform plan 基盤

`ci-test` の Plan 用 CodeBuild、read-only 実行ロール、OIDC ロール、保存 Plan 用 S3 を管理します。この Root 自身は CI/CD 対象外です。State と実行ロールは Apply 基盤から分離し、Pipeline は既存 Apply 基盤の一つだけを共用します。2026-11-10 に AWS 更新と承認済みの旧 Plan Pipeline 1本の廃止を完了し、反映後 Plan は差分なしです。Actions からの実動作検証は残っています。

## 構成

- CodeConnections の既存 GitHub 接続を使い、固定した PR head SHA を Source revision override として取得します。
- 既存 V2 Pipeline の Plan ステージからこの CodeBuild を呼びます。旧 Plan Pipeline は `for_each = {}` と `prevent_destroy` により、新規作成せず既存の意図しない削除も止めます。
- `pull_request_target` workflow は `main` の trusted script だけを実行します。PR のコードを checkout するのは AWS 権限を持たない static-checks job だけです。
- plan role は対象 root の State を読むだけで、State や AWS resource を変更する権限は持ちません。S3 backend のロックファイルだけは作成・削除できます。
- PR head の全 changed files が登録済み root 内にあり、同一 repo の PR である場合だけ plan します。fork PR や基盤変更は AWS plan へ進めません。
- CodeBuild は commit SHA を照合し、`plan -out` を実行します。Plan 本体、照合メタデータ、非公開診断を暗号化・バージョニング済み S3 へ保存し、値を含まない構造化差分だけを CloudWatch Logs へ出します。
- 完了 status は PR head commit に `Terraform plan` context として記録されます。status description に PR base SHA を含め、apply が最新 base 向けの成功 plan か確認します。

## 管理者による構築

Plan 用の 2 Secrets と共通 Pipeline 対応表を用意します。実値は Git 管理外で管理し、既存非公開ファイルはテンプレートと照合してください。

1. `AWS_GITHUB_PLAN_ROLE_ARN` に plan role ARN を登録します。
2. `TF_PLAN_ARTIFACT_BUCKET` に plan artifact bucket 名を登録します。
3. `TF_PIPELINE_NAMES` に既存 Apply 基盤の共通 Pipeline 名を登録します。旧 `TF_PLAN_PIPELINE_NAMES` は不要です。
4. 非公開 tfvars の `source_artifact_bucket_name` に既存 Apply 成果物バケットを設定します。共通 `name_prefix` を維持し、必要なら `shared_pipeline_names` を明示します。
5. 管理者 IAM テンプレートの S3 policy/lifecycle とログ管理権限を確認し、他用途の権限を維持したまま非公開設定へ反映します。6144 文字制限も確認してください。
6. AWS 操作の別途承認後、以下の新しい plan を確認して反映します。旧 Plan Pipeline が State に存在すれば削除保護で停止するため、先に承認済みの移行・廃止手順が必要です。

```bash
terraform -chdir=terraform/env/poc/infra-cicd-plan init -backend-config=backend.hcl -input=false
terraform -chdir=terraform/env/poc/infra-cicd-plan plan -out=infra-plan.tfplan
terraform -chdir=terraform/env/poc/infra-cicd-plan apply infra-plan.tfplan
```

`pull_request_target` は base repository の branch context で動くため、plan role subject は immutable repository ID を含む `ref:refs/heads/main` です。リポジトリを transfer/recreate した場合は owner/repository IDs を再取得し、private tfvars・GitHub plan role trust を同時に更新してください。

## 安全性と運用

- `pull_request_target` workflow は PR のファイルを実行せず、PR API からの metadata のみを読みます。AWS CodeBuild が扱う Terraform は plan 専用 role で動きます。
- Status は対象 head SHA に書かれます。Apply はラベルイベントの PR を使い、成功 Workflow の生成元と S3 の対応記録、head/base を照合します。PR 番号や branch の手入力はありません。
- PR の head や base が変われば新しい plan が必要です。main が進んだときは PR を update branch し、再 plan が成功するまで apply は拒否されます。
- Plan と apply は同じ backend State lock を共有します。同時に動いた場合は Terraform の lock により直列化され、lock timeout は job を失敗させます。
- Summary と status は公開です。Summary は操作種別と変更属性名のみを示し、値・Outputs・生 Plan は出しません。生診断は非公開 S3 に保持します。既知の識別情報も表示時にマスクします。
- Pipeline の Source connection は GitHub PR merge ref の commit ID override を受け取れるか、初回 plan で実動作確認してください。取得 SHA が期待値と異なる場合は CodeBuild が Terraform 前に失敗します。

PR Plan は事前確認用です。Apply Workflow は承認前に別の Plan を保存し、その VersionId と SHA-256 を承認後の適用に使用します。有効期間は 24 時間です。S3 の plans/approvals は現行版・非現行版とも 2 日、diagnostics は 7 日で期限削除し、消費記録は保持します。期限削除は非同期なので、実行時にも期限を検証します。Plan ロールは approvals/consumed に書けず、承認情報の書き手は bucket policy でも Apply 用 OIDC ロールに限定します。

CodeBuild の適用直前確認は公開 GitHub API を読み取り専用で使用します。非公開リポジトリやレート制限等で読めない場合は適用しません。非公開リポジトリに転用する場合は GitHub App 等による限定権限の読み取り認証を別途設計してください。PR 更新と実際の AWS 変更を完全に原子的にはできず、開始済みの適用は自動ロールバックされません。