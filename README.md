# rs-cicd-poc

GitHub Actions と AWS CodePipeline/CodeBuild を組み合わせた Terraform CI/CD の PoC です。

## 構成

| コンポーネント                | 役割                                                                |
| ----------------------------- | ------------------------------------------------------------------- |
| GitHub Actions                | 静的解析、変更 Root 検出、Pipeline 起動、承認、結果表示、自動マージ |
| CodePipeline                  | 検出 Root ごとに1本。Plan/Apply モードで別々に実行                  |
| CodeBuild                     | Plan 用と Apply 用で実行ロールを分離して Terraform を実行           |
| S3                            | Terraform State、ソース成果物、保存 Plan、承認・消費記録            |
| GitHub OIDC / CodeConnections | Actions の AWS 認証 / 指定コミットのソース取得                      |

PR 作成・更新時の `pull_request_target` event で自動 Plan が起動します。`apply-request` ラベルによるApply Workflowも`pull_request_target`で起動し、再PlanとSummary表示の後、Environment `terraform-apply`の承認を経て保存Planを適用します。全対象の成功とPRのSHA・GitHub保護条件を満たす場合だけ自動マージします。

対象Rootは`terraform/env/poc/`直下に`.tf`ファイルを持つディレクトリから自動検出します。`infra-cicd-*`は対象外です。共有module、`terraform/modules/`、`buildspec/`の変更では全対象RootをPlanします。Pipeline名はGitHub Actionsのrepository variable `TF_PIPELINE_NAME_PREFIX`とRootのbasenameから自動生成し、Rootごとの名前mapは使いません。構成の詳細は[terraform/README.md](terraform/README.md)に記載しています。

## 識別情報の管理

AWS の実識別情報は GitHub Secrets と Git 管理外の設定で保持します。`terraform.tfvars`、`backend.hcl`、`*.local.json`、管理者用の実 IAM JSON、State、保存 Plan は Git 管理対象外です。公開設定は `.example` テンプレートです。

Summary には件数・操作種別・変更属性名を表示し、属性値・Outputs・生 Plan は表示しません。既知の識別情報はマスクしますが、加工された値の完全な秘匿までは保証しません。