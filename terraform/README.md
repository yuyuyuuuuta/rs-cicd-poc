# Terraform ディレクトリ構成

```text
terraform/
├── bootstrap/
│   └── state-bucket/       # State 用 S3 の手動構築。管理 State はローカル
└── env/
    └── poc/
        ├── ci-test/        # CI/CD の plan/apply 対象
        ├── infra-cicd-plan/   # Plan 用 CodeBuild/OIDC と保存 Plan S3
        └── infra-cicd-apply/  # Root ごとの共通 Pipeline と Apply CodeBuild
```

`ci-test` と各 CI/CD 基盤は独立した Terraform root とし、State を分離します。対象 root ごとに専用 Pipeline を用意します。基盤自身は CI/CD 対象にせず、管理者が手動で構築・変更します。

添付の案件側 README の `env/poc/` 構成を踏襲します。ただし、ソース取得は S3 ZIP ではなく CodeConnections を使用します。`bootstrap/state-bucket` は、S3 backend を利用する前に保存先を作るための追加 root です。

## 現在の実装

| root                       | 実装内容                                                                 | State                                                     |
| -------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------- |
| `bootstrap/state-bucket`   | S3、バージョニング、暗号化、公開ブロック、所有権設定                     | ローカル `terraform.tfstate`                              |
| `env/poc/ci-test`          | テスト用 S3、暗号化、公開ブロック、同バケットの data source              | S3 `terraform/env/poc/ci-test/terraform.tfstate`          |
| `env/poc/infra-cicd-plan`  | Plan CodeBuild、read-only role、保存 Plan S3。更新・旧 Pipeline 廃止済み | S3 `terraform/env/poc/infra-cicd-plan/terraform.tfstate`  |
| `env/poc/infra-cicd-apply` | 既存 Pipeline を Plan/Apply 兼用化。AWS 反映・差分なし確認済み           | S3 `terraform/env/poc/infra-cicd-apply/terraform.tfstate` |

各 root の `_terraform.tf` は CLI・provider の制約、`_providers.tf` は接続先、`_backend.tf` は公開可能な State キー、`_variables.tf` は入力変数を定義します。実際のアカウント・リージョン・バケット名は、Git 管理外の `terraform.tfvars` と `backend.hcl` に置きます。ファイル名の `_` は読み込み順を制御しません。

## 非公開設定

各 root の `terraform.tfvars.example` を `terraform.tfvars` として配置し、実環境の値を設定します。S3 backend を使う root は `backend.hcl.example` を `backend.hcl` として配置します。これらの実値ファイルは Git 管理対象外です。既存環境の値はローカルに保持しており、匿名化のためにリソース名や State キーを変更する必要はありません。

CodeBuild では管理者が登録した環境変数で Terraform 入力と backend 設定を渡します。PR 内に実値の tfvars を追加する必要はありません。GitHub Actions の静的検証ジョブには、AWS の識別情報 Secrets も渡しません。

## AWS を変更しない検証

リポジトリのルートで実行します。Terraform CLI は `1.16.x` を使用します。

```bash
terraform fmt -check -recursive terraform
terraform -chdir=terraform/bootstrap/state-bucket init -backend=false -input=false
terraform -chdir=terraform/bootstrap/state-bucket validate
terraform -chdir=terraform/env/poc/ci-test init -backend=false -input=false
terraform -chdir=terraform/env/poc/ci-test validate
terraform -chdir=terraform/env/poc/infra-cicd-apply init -backend=false -input=false
terraform -chdir=terraform/env/poc/infra-cicd-apply validate
terraform -chdir=terraform/env/poc/infra-cicd-plan init -backend=false -input=false
terraform -chdir=terraform/env/poc/infra-cicd-plan validate
```

## 手動ブートストラップ

State 保存基盤は非公開設定で指定したアカウント・リージョンに構築済みです（5 リソース作成）。以下は設定ファイルを準備した後の手順です。

```bash
aws sts get-caller-identity
terraform -chdir=terraform/bootstrap/state-bucket init -input=false
terraform -chdir=terraform/bootstrap/state-bucket plan -out=bootstrap.tfplan
terraform -chdir=terraform/bootstrap/state-bucket apply bootstrap.tfplan
```

State バケット名は `state_bucket_name` と Secret `TF_STATE_BUCKET` で管理します。バージョニング、AES256 暗号化、公開アクセスの全ブロック、BucketOwnerEnforced を AWS API で確認済みです。管理 State は S3 自身に移さず、`bootstrap/state-bucket/terraform.tfstate` を安全な場所にバックアップして保管してください。この State とバックアップは Git 管理対象外です。

対象 root の S3 backend 初期化と validate は実施済みです。次のコマンドで再初期化・plan できます。

```bash
terraform -chdir=terraform/env/poc/ci-test init -backend-config=backend.hcl -input=false
terraform -chdir=terraform/env/poc/ci-test plan
```

`ci-test` の手動 apply はここでは行いません。ラベル起動の GitHub Actions と共通 Pipeline の検証対象です。State バケットには `prevent_destroy`、テスト用バケットには `force_destroy = false` を設定しています。

旧 Connection は削除済みで、新しい Connection は GitHub 接続承認済みです。AWS API で AVAILABLE を確認しました。

2026-11-10 に共通 Pipeline・保存 Plan・S3 保護設定を AWS へ反映し、承認済みの旧 Plan Pipeline 1本を廃止しました。既存資源の再作成はなく、各基盤の反映後 Plan は差分なしです。次は GitHub 設定確認、コードの main 反映、検証 PR による一連の動作確認です。手順は [plan 基盤 README](env/poc/infra-cicd-plan/README.md) と [apply 基盤 README](env/poc/infra-cicd-apply/README.md) を参照してください。