# rs-cicd-poc

GitHub Actions と AWS CodePipeline を組み合わせた Terraform apply の PoC です。

現在は State 保存基盤と `ci-test` 専用 apply 基盤の AWS 構築が完了し、Connection は AVAILABLE です。PR 自動 plan と branch 指定の手動 apply workflow を実装しています。plan 基盤は Terraform code/validate と 13 resource create plan まで検証済みですが、AWS apply、追加 Secret 登録、workflow 実行は未実施です。

構成と現在の手順は [terraform/README.md](terraform/README.md) を参照してください。

## 手動 Apply

plan 実行用の OIDC role/pipeline/artifact Secrets を登録し、plan 基盤を管理者が手動構築します。apply 側は Environment `terraform-apply` の Required reviewers と deployment branch `main` 制限を設定してください。初回配置や基盤変更の PR は apply workflow の対象にしません。

push 後に PR を作成すると自動 plan が動きます。完了後、Actions の `Terraform apply PR (manual)` → Run workflow → Branch `main` から PR の source branch を指定します。静的解析後に Environment 承認を待ち、承認後に専用 Pipeline を起動します。Actions がログを Summary に整形し、成功と HEAD/base の不変を確認して自動マージします。

fork PR、基盤や workflow を変更する PR、失敗・タイムアウト時は apply/merge を止めます。AWS に加えた変更は自動ロールバックされません。apply は head/base に結び付いた成功 plan status を要求します。詳細は plan/apply 各 README を参照してください。

## 識別情報の管理

アカウント ID、実バケット名、実ロール ARN、Pipeline 名、Connection ARN を公開ファイルへ記載しません。公開するのは `.example` テンプレートだけです。`terraform.tfvars`、`backend.hcl`、`*.local.json`、管理者用の実 IAM JSON、State、保存 plan は Git 管理対象外です。`git add -f` で除外を迂回しないでください。

GitHub Actions では Secrets、管理者のローカル Terraform 操作では Git 管理外の設定を使います。AWS 内の CodeBuild 設定・Terraform State にも実値は保持されますが、公開リポジトリへは含めません。

ログは既知の実値、ARN、アカウント ID、UUID を表示時にマスキングします。ただし、加工・分割された値まで完全に防ぐ保証はありません。公開リポジトリ自身の owner 名・リポジトリ名やコミット履歴は Secrets では隠せません。過去に識別情報を push していた場合、今回の変更だけでは履歴から消えません。