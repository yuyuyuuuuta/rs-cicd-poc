# Terraform plan 基盤

`ci-test` 専用の自動 plan 基盤です。この Terraform root 自身は CI/CD 対象にせず、管理者が手動で構築します。plan 基盤の State は apply 基盤と別 key に保存し、CodeBuild の実行ロールも分離します。

## 構成

- CodeConnections の既存 GitHub 接続を使い、固定した PR head SHA を Source revision override として取得します。
- `ci-test` 専用 V2 CodePipeline を用意します。実際の pipeline 名は管理者の `terraform output -json pipeline_names` で確認してください。
- `pull_request_target` workflow は `main` の trusted script だけを実行します。PR のコードを checkout するのは AWS 権限を持たない static-checks job だけです。
- plan role は対象 root の State を読むだけで、State や AWS resource を変更する権限は持ちません。S3 backend のロックファイルだけは作成・削除できます。
- PR head の全 changed files が登録済み root 内にあり、同一 repo の PR である場合だけ plan します。fork PR や基盤変更は AWS plan へ進めません。
- CodeBuild はソースの commit SHA と要求 SHA を照合し、`terraform plan` のログを CloudWatch Logs に出します。
- 完了 status は PR head commit に `Terraform plan` context として記録されます。status description に PR base SHA を含め、apply が最新 base 向けの成功 plan か確認します。

## 管理者による構築

plan 実行用の 3 resource Secrets と IAM policy を先に用意します。値は Git 管理外の `admin/github-secrets.local.json` と private Terraform 設定にあります。

1. `AWS_GITHUB_PLAN_ROLE_ARN` に plan role ARN を登録します。
2. `TF_PLAN_ARTIFACT_BUCKET` に plan artifact bucket 名を登録します。
3. `TF_PLAN_PIPELINE_NAMES` に `{"terraform/env/poc/ci-test":"<PIPELINE_NAME>"}` を JSON 文字列として登録します。
4. `admin/iam/terraform-cicd-policy.json.example` の placeholder を実値にし、管理者の構築用 IAM policy に plan artifact bucket と plan log group の許可を追加します。既存の権限は削除せず、6144 文字制限も確認してください。
5. `LegoSSM` で次を実行し、plan を確認してから apply します。

```bash
terraform -chdir=terraform/env/poc/infra-cicd-plan init -backend-config=backend.hcl -input=false
terraform -chdir=terraform/env/poc/infra-cicd-plan plan -out=infra-plan.tfplan
terraform -chdir=terraform/env/poc/infra-cicd-plan apply infra-plan.tfplan
```

`pull_request_target` は base repository の branch context で動くため、plan role subject は immutable repository ID を含む `ref:refs/heads/main` です。リポジトリを transfer/recreate した場合は owner/repository IDs を再取得し、private tfvars・GitHub plan role trust を同時に更新してください。

## 安全性と運用

- `pull_request_target` workflow は PR のファイルを実行せず、PR API からの metadata のみを読みます。AWS CodeBuild が扱う Terraform は plan 専用 role で動きます。
- Status は対象 head SHA に書かれ、apply は PR branch から PR を一意に解決して、現在 open な同一 repo の PR、head/base、成功 status の base SHA を再確認します。branch と PR 番号を手動で別々に渡す方式ではありません。
- PR の head や base が変われば新しい plan が必要です。main が進んだときは PR を update branch し、再 plan が成功するまで apply は拒否されます。
- Plan と apply は同じ backend State lock を共有します。同時に動いた場合は Terraform の lock により直列化され、lock timeout は job を失敗させます。
- GitHub Actions の plan log と status description は公開です。ログは known bucket, pipeline, ARN, account, UUID を mask しますが、PR の Terraform 入出力に secret を書かないでください。
- Pipeline の Source connection は GitHub PR merge ref の commit ID override を受け取れるか、初回 plan で実動作確認してください。取得 SHA が期待値と異なる場合は CodeBuild が Terraform 前に失敗します。

Plan status は差分 artifact を保管・署名したものではなく、commit/base に対する成功記録です。plan 成功後に AWS 側状態が変化した場合、apply 時の Terraform plan は異なる可能性があるため、Environment 承認者はログと経過時間を考慮してください。より厳密な差分承認が必要なら、saved plan の暗号化・短期 artifact 保管方式を別途設計します。