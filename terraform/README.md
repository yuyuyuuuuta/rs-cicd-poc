# Terraform ディレクトリ構成

```text
terraform/
├── bootstrap/
│   └── state-bucket/          # State 用 S3。管理 State はローカル
└── env/
    └── poc/
    ├── ci-test/          # CI/CD 対象の S3 Root
    ├── infra-cicd-plan/  # Plan 用 CodeBuild/OIDC と保存 Plan S3
    └── infra-cicd-apply/ # 共通 Pipeline と Apply 用 CodeBuild/OIDC
```

各ディレクトリは独立した Terraform Root で、State を分離します。CI/CD 対象 Root ごとに1本の共通 Pipeline を持ち、Plan 用・Apply 用 CodeBuild は別ロールで動作します。基盤 Root 自身は自動適用の対象外です。

## Root と State

| Root                       | 管理対象                                                                     | State                                                     |
| -------------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------- |
| `bootstrap/state-bucket`   | State 用 S3、暗号化、バージョニング、公開ブロック、TLS 必須ポリシー          | ローカル `terraform.tfstate`                              |
| `env/poc/ci-test`          | 検証用 S3、暗号化、公開ブロック、同バケットの data source                    | S3 `terraform/env/poc/ci-test/terraform.tfstate`          |
| `env/poc/infra-cicd-plan`  | Plan 用 CodeBuild/IAM/OIDC、保存 Plan 用 S3                                  | S3 `terraform/env/poc/infra-cicd-plan/terraform.tfstate`  |
| `env/poc/infra-cicd-apply` | 共通 Pipeline、Apply 用 CodeBuild/IAM/OIDC、CodeConnections、ソース成果物 S3 | S3 `terraform/env/poc/infra-cicd-apply/terraform.tfstate` |

S3 backend は暗号化と `use_lockfile = true` を使用します。State バケットはバージョニング、公開アクセスブロック、BucketOwnerEnforced、`prevent_destroy` を持ちます。外部付与の `Owner` タグは Terraform の更新対象外です。

## 設定の分離

各 Root の `_terraform.tf` は CLI/provider 制約、`_providers.tf` は接続先、`_backend.tf` は State キー、`_variables.tf` は入力変数を定義します。

| 設定                                 | 保持先                                          |
| ------------------------------------ | ----------------------------------------------- |
| ローカルの実値入力                   | Git 管理外の `terraform.tfvars` / `backend.hcl` |
| Actions の実識別情報                 | GitHub Secrets                                  |
| CodeBuild の Terraform 入力・backend | Terraform 管理の環境変数                        |
| 公開可能な設定例                     | `.example` ファイル                             |

静的解析ジョブには AWS 認証情報・識別情報 Secrets を渡しません。両 CI/CD 基盤の相互参照は、Apply 側の `plan_artifact_bucket_name` と Plan 側の `source_artifact_bucket_name` です。

詳細は [Plan 基盤](env/poc/infra-cicd-plan/README.md) と [Apply 基盤](env/poc/infra-cicd-apply/README.md) に記載しています。