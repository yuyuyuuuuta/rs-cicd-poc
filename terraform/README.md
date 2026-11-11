# Terraform ディレクトリ構成

```text
terraform/
├── bootstrap/
│   └── state-bucket/          # State 用 S3。管理 State はローカル
└── env/
    └── poc/
        ├── <root>/              # .tfを含むCI/CD対象Root
        ├── infra-cicd-plan/     # Plan用CodeBuild/OIDCと保存Plan S3
        └── infra-cicd-apply/    # 共通PipelineとApply用CodeBuild/OIDC
```

各ディレクトリは独立したTerraform Rootで、Stateを分離します。`terraform/env/poc/`直下の`.tf`ファイルを持つディレクトリを自動検出し、`infra-cicd-*`は対象から除外します。対象Rootごとに1本の共通Pipelineを生成し、Plan用・Apply用CodeBuildは別ロールで動作します。

## Root と State

| Root                       | 管理対象                                                                     | State                                                     |
| -------------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------- |
| `bootstrap/state-bucket`   | State 用 S3、暗号化、バージョニング、公開ブロック、TLS 必須ポリシー          | ローカル `terraform.tfstate`                              |
| `env/poc/<root>`           | `.tf`を含む検出対象Root                                                      | S3 `terraform/env/poc/<root>/terraform.tfstate`           |
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

Pipeline名はGitHub Actionsのrepository variable `TF_PIPELINE_NAME_PREFIX`を共通prefixとして、`<prefix>-<root basename>-apply`から生成します。RootごとのPipeline名mapやSecretは不要です。

詳細は [Plan 基盤](env/poc/infra-cicd-plan/README.md) と [Apply 基盤](env/poc/infra-cicd-apply/README.md) に記載しています。