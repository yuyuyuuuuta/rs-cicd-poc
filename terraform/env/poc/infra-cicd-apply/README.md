# Terraform apply 基盤

`ci-test` 専用の apply 基盤です。14/14 リソースの AWS 構築が完了し、構築後の plan は差分なしです。State は S3 に保持しています。Connection は GitHub 接続承認済みで、AWS API でも AVAILABLE を確認しました。

Connection、成果物 S3 と付随設定、3 つの IAM ロール、ロググループ、CodeBuild、CodePipeline、3 つのインラインポリシーは作成済みです。今後の変更時も既存 State を維持し、古い保存 plan を再利用せず plan を取り直してください。

## 実装内容

- Pipeline: `TF_PIPELINE_NAMES` Secret で指定します。Source → TerraformApply の 2 ステージです。
- Source: CodeConnections、非公開 tfvars の `github_repository`、通常ブランチ `main`、変更検知による自動起動なし。
- 対象: `terraform/env/poc/ci-test` に固定し、基盤 root は対象に含めません。
- 実行: V2 Pipeline の QUEUED モードで、同じ root の実行を直列化します。
- 権限: 専用 CodeBuild ロールはテスト用バケットと対象 State キーだけを書き換えます。管理者権限は付与しません。
- ログ: 実ロググループ名は管理者の `terraform output` で確認します。保存期間 14 日。
- GitHub 用ロール: Pipeline 起動・状態確認と CodeBuild／CloudWatch Logs の読み取りだけを許可します。

対象 root の追加は `_data.tf` の `local.targets` に登録します。各対象に専用 Pipeline・CodeBuild・サービスロール・ロググループを作ります。対象リソースが S3 以外の場合、専用 CodeBuild ポリシーと buildspec のパス規則も見直してください。

## 実行時の安全条件

起動する側が Source アクションに `COMMIT_ID` の revision override を指定し、Pipeline 変数 `PrCommitSha` に同じ PR HEAD SHA を渡します。CodeBuild は Source アクションが返した `CommitId` と照合します。未指定・不一致は Terraform 実行前に失敗します。

buildspec は管理者が Terraform で CodeBuild に登録するインライン設定です。PR 内の buildspec 変更だけでは実行手順を変更できません。変更を反映するには管理者の基盤 apply が必要です。

ログには `TERRAFORM_APPLY_BEGIN` / `TERRAFORM_APPLY_END` と Terraform 終了コードを出力します。初期化・validate・apply の失敗も同じログ区間に含みます。Actions 側の待機・ログ取得・整形・自動マージは実装済みで、ローカルの模擬テストを通過しています。実環境での一連の動作はまだ未検証です。

PR 内の Terraform 自体はコードを実行できるため、SHA 照合は安全性の審査を代替しません。fork PR を権限付きで実行せず、担当者が同一リポジトリの PR を確認してから手動起動する前提です。

## 管理者による構築

リポジトリのルートで実行します。基盤自身は CI/CD 対象にしません。

```bash
terraform -chdir=terraform/env/poc/infra-cicd-apply init -backend-config=backend.hcl -input=false
terraform -chdir=terraform/env/poc/infra-cicd-apply plan -out=infra-apply.tfplan
terraform -chdir=terraform/env/poc/infra-cicd-apply apply infra-apply.tfplan
```

CodeConnections、CodePipeline、CodeBuild、IAM、S3、CloudWatch Logs の構築権限が必要です。現在の Connection ARN は旧形式の `arn:aws:codestar-connections:...` なので、Pipeline 作成時に AWS が旧権限名 `codestar-connections:PassConnection` を要求しました。この不足は修正済みで、Pipeline 作成は成功しています。

整理 JSON の `PassPocConnectionToPipeline` の `Action` は `codestar-connections:PassConnection`、引き渡し先の条件キーは `codestar-connections:PassedToService` です。対象は同じ Connection ARN、引き渡し先は CodePipeline だけです。AWS のカスタムポリシーへの反映後、残り 2 リソースの作成が完了しました。

### 構築用カスタムポリシーの整理

公開する置き換え用テンプレートは [admin/iam/terraform-cicd-policy.json.example](../../../../admin/iam/terraform-cicd-policy.json.example) です。プレースホルダーを実値へ置換した JSON は Git 管理外で保管し、AWS に登録します。実ポリシーの検証時は、空白を除く文字数がカスタマー管理ポリシーの上限 6144 文字以内であることを確認してください。空白の削除自体では上限対策になりません。

整理対象は Terraform 基盤構築用カスタムポリシーだけです。管理者ロールに付与されている別の IAM ロール管理、PassRole、EC2/SSM 関連ポリシーを置き換えたり削除したりしないでください。CodeBuild の実行ロール用ポリシーでもありません。

| 範囲                 | 整理内容                                                                                                                  |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| S3                   | 新 PoC の State、apply 成果物、ci-test の 3 バケットに限定。バケット操作とオブジェクト操作を分離                          |
| Pipeline / CodeBuild | 非公開設定のリージョンとリソース名プレフィックスに限定。旧 plan 専用権限と重複する一覧権限を除去                          |
| Connection           | 作成は GitHub・対象リージョンに限定。管理と PassConnection は作成済み Connection に限定し、引き渡し先は CodePipeline のみ |
| Connection タグ      | 作成時は ARN が未確定なので、対象アカウント・リージョンの Connection ARN に限定してタグ付けを許可                         |
| Logs                 | 現在の apply ロググループの作成・保存期間・タグ・削除権限を統合                                                           |
| サービス一覧         | Resource を指定できない API は `Resource: "*"` とし、RequestedRegion 条件で対象リージョンに限定                           |

旧バケットの削除・旧 CodeBuild のビルド参照・削除済み Connection 専用権限は除去しました。旧 policy の `ListConnections` は一覧 API に不適合な Resource 指定だったため、リージョン条件付き一覧権限に修正しています。この API はリポジトリ単位の一覧制限をできず、同リージョンの Connection 一覧を参照できます。

新 PoC の S3 バケット削除、旧バージョン削除、新 CodeBuild のビルド実行・ビルドログ取得をこの構築用ポリシーに追加してはいません。現在の構築手順に不要なためです。撤去や管理者による直接実行が必要になったら、対象を限定して別途追加します。Pipeline・CodeBuild・GitHub Actions の専用実行ロールの権限は Terraform の `iam.tf` で別管理します。

### ポリシーの適用

ログ管理権限と PassConnection 修正は AWS に反映済みで、基盤の作成完了を確認しました。JSON 構文・文字数・対象 ARN と条件もローカルで検証済みです。以下はポリシーを今後更新する場合の手順です。

1. 既存カスタムポリシーが他ロール・ユーザーにも付与されていないか確認します。共有されている場合は、そのポリシー全体を変更せず、構築用ロール専用ポリシーとして作成します。
2. 既存ポリシーをバックアップしてから、テンプレートを実値へ置換した JSON を IAM に登録します。実値 JSON を公開リポジトリへ含めないでください。
3. 新しいバージョンをデフォルトにし、構築用ロールに有効なポリシーとして付与されていることを確認します。
4. 新規ポリシーを使う場合、元のポリシーが残っていると権限は合算されます。今回の整理対象だけを外し、他用途のポリシーは残します。
5. ログ用ポリシーを別途作成済みの場合、新案の反映後に二重付与を整理します。

新しい Connection を作り直した場合は、JSON 内の管理・PassConnection の ARN を実際の ARN に更新してください。plan 基盤や対象 root を追加する際も、S3 と Logs の許可対象を追加する必要があります。

Connection の現在の状態は AVAILABLE です。実 ARN は管理者の `terraform output` と非公開 State で確認します。実際の PR コミット取得は Pipeline 実行時に別途検証します。

Connection を作り直す場合は、作成直後に PENDING になります。AWS コンソールの CodeConnections で GitHub App の接続を承認し、このリポジトリへのアクセスを許可して AVAILABLE にしてください。Terraform のリソース名 `aws_codestarconnections_connection` は provider 上の名称で、使用するサービスは CodeConnections です。

## 後続の GitHub 設定

既存の OIDC ロール ARN に加えて、次の全 6 Secret を Settings → Secrets and variables → Actions → New repository secret に設定してください。実値は Git 管理外の `admin/github-secrets.local.json` に保持しています。新しい作業環境では [設定テンプレート](../../../../admin/github-secrets.local.json.example) を使用します。識別情報や認証情報をチャットへ貼り付ける必要はありません。

| Secret 名                     | 値の取得元                                                                                         |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| `AWS_GITHUB_ACTIONS_ROLE_ARN` | `terraform output -raw github_actions_role_arn`。登録済みの値を維持                                |
| `AWS_ACCOUNT_ID`              | 非公開 tfvars の `aws_account_id`                                                                  |
| `AWS_REGION`                  | 非公開 tfvars の `aws_region`                                                                      |
| `TF_STATE_BUCKET`             | 非公開 tfvars の `state_bucket_name`                                                               |
| `TF_TEST_BUCKET`              | 非公開 tfvars の `test_bucket_name`                                                                |
| `TF_PIPELINE_NAMES`           | root パスをキー、Pipeline 名を値とする JSON。例: `{"terraform/env/poc/ci-test":"<PIPELINE_NAME>"}` |

`TF_PIPELINE_NAMES` の実 Pipeline 名は `terraform output -json pipeline_names` で管理者が確認します。出力のキーは `ci-test` なので、Secret のキーには実パス `terraform/env/poc/ci-test` を指定してください。JSON 文字列を Secret の値として登録します。

長期 AWS アクセスキー、Secret Access Key、セッショントークンは登録しません。認証は OIDC で行います。GitHub 操作用の `GITHUB_TOKEN` は Actions 実行時に自動提供されます。リポジトリの設定・組織ポリシーで、使用 action とマージ専用ジョブの書き込み権限が許可されている必要があります。

静的検証ジョブには識別情報 Secrets を渡しません。AWS ジョブの trusted なコードだけが設定確認と起動・ログ取得を行います。CodeBuild には管理者が Terraform で登録した環境変数として識別情報を渡します。これらの値と State は AWS 側で非公開管理し、Git に含めません。

OIDC の信頼条件は `repo:<OWNER>/<REPOSITORY>:ref:refs/heads/main` です。実際のリポジトリは非公開 tfvars から設定します。Environment やカスタム subject を使う場合は信頼条件も変更してください。

## 手動 Workflow

workflow は [.github/workflows/terraform-apply.yml](../../../../.github/workflows/terraform-apply.yml)、補助コードは [.github/scripts/terraform_apply.py](../../../../.github/scripts/terraform_apply.py) です。`workflow_dispatch` だけを使用し、PR 作成・push では起動しません。現在の登録対象は `ci-test` だけです。

1. 初回は管理者が今回のコード一式をレビューし、workflow と補助スクリプトを含めて `main` に commit・push します。初回配置用 PR や基盤変更 PR を apply workflow でマージしようとしないでください。
2. `terraform/env/poc/ci-test/` を変更する同一リポジトリの PR を、`main` 向けに作成します。共同変更を許可する対象外ファイルはルート README と Terraform 全体 README だけです。基盤、IAM、workflow、未登録 root の変更を含めると停止します。
3. Actions → `Terraform apply PR (manual)` → Run workflow で Branch に `main`、`pr_number` に対象 PR 番号を指定します。起動時点の `main` が進んでいた場合や GitHub が mergeability を計算中の場合は、情報が確定してから再実行します。
4. `inspect` が open・非 draft・同一リポジトリ・マージ可能性を確認します。`static-checks` は AWS 権限なしで固定 SHA を checkout し、fmt・backend 無効 init・validate を実行します。provider lock の変更は自動許可せず、PR の lock ファイルを使用します。
5. `aws-apply` は trusted な `main` の補助コードだけを実行し、OIDC 認証と AWS アカウント照合後、SHA override 付きで専用 Pipeline を起動します。Pipeline 成功後も、SourceOutput の revision SHA と CodeBuild 成功を確認します。
6. CloudWatch Logs を全ページ取得し、マーカー間のログを Summary に掲載します。空行・ANSI 色を除去し、大きいログは表示を切り詰めます。ログ取得失敗やマーカー不足でもマージしません。
7. `merge` 専用ジョブが PR HEAD と base を再確認し、GitHub の merge API に適用済み HEAD SHA を指定してマージします。ブランチ保護を迂回する API や管理者オプションは使用しません。

workflow 全体をリポジトリ単位で直列化し、実行中の apply を新しい起動でキャンセルしない設定です。GitHub concurrency の待機枠には上限があるため、連続起動時は未実行の待機 run が置き換わる場合があります。Pipeline 側も root ごとの QUEUED モードで直列化しています。

Pipeline の待機上限は 40 分、AWS ジョブの上限は 55 分です。失敗・タイムアウト・ログ取得失敗・実行中の HEAD/base 更新ではマージしません。ただし、すでに行われた AWS 変更はロールバックされず、GitHub の run をキャンセルしても AWS の実行は自動停止しません。AWS の状態を確認してから再実行してください。

GitHub API は HEAD SHA の一致をマージ時に検証しますが、base の確認と merge API は単一トランザクションではありません。base の更新を厳密に制御する運用では、main の変更制御と適切な branch rules も必要です。必須レビュー・必須チェック・許可されたマージ方式は GitHub の設定に従い、拒否された場合は workflow が失敗します。マージ方式には merge commit を使用します。

この公開リポジトリでは Actions Summary も公開されます。既知のバケット名・Pipeline 名・リージョン、ARN、アカウント ID、UUID は表示前にマスキングし、AWS 認証 action でもアカウント ID を伏せます。全生ログの Artifact アップロードは行いません。Secrets の登録だけでログの漏えいが完全に防げるわけではなく、加工・分割された値や未知の識別子までは保証できません。PR の Terraform コード自体もレビューしてください。GitHub の owner 名・リポジトリ名や既存履歴は公開リポジトリの性質上隠せません。

## ローカル検証

リポジトリのルートで、外部 API や Terraform apply を呼ばずに検証できます。

```bash
python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
```

35 テストと actionlint で検証済みです。PR 判定、SHA 更新時の停止、Pipeline 待機、ログのページング、取得失敗時の停止、固定 SHA マージ、Secret の設定確認と識別子のマスキングに加え、公開ファイルのアカウント ID・認証情報の混入を検査します。CodeBuild の環境変数更新は反映済みで、更新後の plan は差分なしです。実際の OIDC・PR コミット取得・apply・自動マージは、追加 Secret の登録と main への初回配置後に別途検証します。plan 基盤は未実装です。