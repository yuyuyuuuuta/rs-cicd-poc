# rs-cicd-poc

GitHub Actions と AWS CodePipeline/CodeBuild を組み合わせた Terraform CI/CD の PoC です。

2026-11-10: State 保護、保存 Plan 基盤、既存 Pipeline の Plan/Apply 共通化を AWS に反映しました。承認済みの旧 Plan 専用 Pipeline 1本は廃止済みで、各基盤の反映後 Plan は差分なしです。GitHub 設定確認・今回のコードの main 反映・実 PR による一連の動作検証は残っています。

構成と現在の手順は [terraform/README.md](terraform/README.md) を参照してください。

## PR Plan と承認付き Apply

Plan 用 OIDC role/CodeBuild/artifact 基盤を用意し、既存 Apply Pipeline を Root ごとに一つの共通 Pipeline として更新します。Plan/Apply の実行ロールは分離します。Environment `terraform-apply` に Required reviewers、自己承認防止、deployment branch `main` 制限、管理者バイパス禁止を設定してください。初回配置や基盤変更の PR は通常の CI/CD 対象にしません。

PR 作成・更新時に自動 Plan が動きます。成功後に write 以上の権限を持つ担当者が PR へ `apply-request` を付けると、静的解析と最新 Plan の再作成を実行します。Summary の差分を確認して Environment 承認すると、その保存 Plan だけを AWS 側で適用します。全 Root の成功と HEAD/base、GitHub 保護条件を確認して自動マージします。PR 番号・ブランチ名の手入力は不要です。

fork PR、基盤や workflow を変更する PR、失敗・タイムアウト時は Apply/Merge を止めます。PR 更新・ラベル解除・PR 終了は古い Workflow をキャンセルしますが、実行中の AWS 操作は取り消しません。AWS 側でも適用直前に PR と Workflow を再確認します。現在は公開 GitHub リポジトリの読み取り API を使用するため、非公開リポジトリでは fail closed になります。

Plan の有効期間は 24 時間です。期限切れ・古い State は再 Plan と再承認が必要です。同じ PR/SHA/Root の適用許可は一度だけ消費でき、失敗・部分適用後は管理者が State と S3 消費記録を確認します。詳細は plan/apply 各 README を参照してください。

## 識別情報の管理

アカウント ID、実バケット名、実ロール ARN、Pipeline 名、Connection ARN を公開ファイルへ記載しません。公開するのは `.example` テンプレートだけです。`terraform.tfvars`、`backend.hcl`、`*.local.json`、管理者用の実 IAM JSON、State、保存 plan は Git 管理対象外です。`git add -f` で除外を迂回しないでください。

GitHub Actions では Secrets、管理者のローカル Terraform 操作では Git 管理外の設定を使います。AWS 内の CodeBuild 設定・Terraform State にも実値は保持されますが、公開リポジトリへは含めません。

ログは既知の実値、ARN、アカウント ID、UUID を表示時にマスキングします。ただし、加工・分割された値まで完全に防ぐ保証はありません。公開リポジトリ自身の owner 名・リポジトリ名やコミット履歴は Secrets では隠せません。過去に識別情報を push していた場合、今回の変更だけでは履歴から消えません。