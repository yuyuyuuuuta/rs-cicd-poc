# rs-cicd-poc

GitHub Actions と AWS CodePipeline を組み合わせた Terraform apply の PoC です。

現在は State 保存基盤と `ci-test` 専用 apply 基盤の AWS 構築、Secrets 登録、初回 workflow の main 配置が完了しています。apply 基盤は 14 リソース作成済み、Connection は AVAILABLE です。apply 前に `terraform-apply` Environment の承認を要求する変更を追加し、AWS の OIDC 信頼条件も反映済みです。GitHub 側の保護ルール設定と今回の変更の main 反映、実際の OIDC 認証・apply・自動マージは未実施です。plan 基盤はまだ実装していません。

構成と現在の手順は [terraform/README.md](terraform/README.md) を参照してください。

## 手動 Apply

初回は [apply 基盤の設定手順](terraform/env/poc/infra-cicd-apply/README.md) に従って 6 つの Repository Secrets と Environment `terraform-apply` の Required reviewers・`main` 制限を設定します。保護ルールの設定と承認ゲートのコードを `main` に配置するまでは実行しないでください。初回配置や基盤変更の PR は apply workflow の対象にしません。

その後、`terraform/env/poc/ci-test/` の変更 PR を作成し、Actions の `Terraform apply PR (manual)` → Run workflow → Branch: `main` で PR 番号を指定します。fmt・validate 後に Environment 承認を待ち、承認後にだけ専用 Pipeline を起動します。Actions がログを取得・整形し、成功と HEAD・base の不変を確認してから、同じ HEAD SHA を指定してマージします。

fork PR、基盤や workflow を変更する PR、実行中に更新された PR、失敗・タイムアウト時は自動マージしません。AWS に加えた変更は自動ロールバックされません。詳細と前提設定は [apply 基盤の README](terraform/env/poc/infra-cicd-apply/README.md) を参照してください。

## 識別情報の管理

アカウント ID、実バケット名、実ロール ARN、Pipeline 名、Connection ARN を公開ファイルへ記載しません。公開するのは `.example` テンプレートだけです。`terraform.tfvars`、`backend.hcl`、`*.local.json`、管理者用の実 IAM JSON、State、保存 plan は Git 管理対象外です。`git add -f` で除外を迂回しないでください。

GitHub Actions では Secrets、管理者のローカル Terraform 操作では Git 管理外の設定を使います。AWS 内の CodeBuild 設定・Terraform State にも実値は保持されますが、公開リポジトリへは含めません。

ログは既知の実値、ARN、アカウント ID、UUID を表示時にマスキングします。ただし、加工・分割された値まで完全に防ぐ保証はありません。公開リポジトリ自身の owner 名・リポジトリ名やコミット履歴は Secrets では隠せません。過去に識別情報を push していた場合、今回の変更だけでは履歴から消えません。