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
| S3                   | 新 PoC の State、plan/apply 成果物、ci-test の 4 バケットに限定。バケット操作とオブジェクト操作を分離                     |
| Pipeline / CodeBuild | 非公開設定のリージョンとリソース名プレフィックスに限定。旧 plan 専用権限と重複する一覧権限を除去                          |
| Connection           | 作成は GitHub・対象リージョンに限定。管理と PassConnection は作成済み Connection に限定し、引き渡し先は CodePipeline のみ |
| Connection タグ      | 作成時は ARN が未確定なので、対象アカウント・リージョンの Connection ARN に限定してタグ付けを許可                         |
| Logs                 | plan/apply の各ロググループの作成・保存期間・タグ・削除権限を限定                                                         |
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

apply 用 6 Secrets と plan 用 3 Secrets を Settings → Secrets and variables → Actions → New repository secret に設定してください。実値は Git 管理外の `admin/github-secrets.local.json` に保持しています。新しい作業環境では [設定テンプレート](../../../../admin/github-secrets.local.json.example) を使用します。識別情報や認証情報をチャットへ貼り付ける必要はありません。

| Secret 名                     | 値の取得元                                                                                         |
| ----------------------------- | -------------------------------------------------------------------------------------------------- |
| `AWS_GITHUB_ACTIONS_ROLE_ARN` | `terraform output -raw github_actions_role_arn`。登録済みの値を維持                                |
| `AWS_ACCOUNT_ID`              | 非公開 tfvars の `aws_account_id`                                                                  |
| `AWS_REGION`                  | 非公開 tfvars の `aws_region`                                                                      |
| `TF_STATE_BUCKET`             | 非公開 tfvars の `state_bucket_name`                                                               |
| `TF_TEST_BUCKET`              | 非公開 tfvars の `test_bucket_name`                                                                |
| `TF_PIPELINE_NAMES`           | root パスをキー、Pipeline 名を値とする JSON。例: `{"terraform/env/poc/ci-test":"<PIPELINE_NAME>"}` |
| `AWS_GITHUB_PLAN_ROLE_ARN`    | plan 基盤の `terraform output -raw github_plan_role_arn`                                           |
| `TF_PLAN_ARTIFACT_BUCKET`     | plan 基盤の非公開 tfvars の `artifact_bucket_name`                                                 |
| `TF_PLAN_PIPELINE_NAMES`      | root パスから plan Pipeline 名への JSON map                                                        |

`TF_PIPELINE_NAMES` と `TF_PLAN_PIPELINE_NAMES` の実 Pipeline 名は各 root の `terraform output -json pipeline_names` で管理者が確認します。出力のキー `ci-test` は JSON Secret では実パス `terraform/env/poc/ci-test` に対応付けます。JSON 文字列として登録してください。

長期 AWS アクセスキー、Secret Access Key、セッショントークンは登録しません。認証は OIDC で行います。GitHub 操作用の `GITHUB_TOKEN` は Actions 実行時に自動提供されます。リポジトリの設定・組織ポリシーで、使用 action とマージ専用ジョブの書き込み権限が許可されている必要があります。

静的検証ジョブには識別情報 Secrets を渡しません。AWS ジョブの trusted なコードだけが設定確認と起動・ログ取得を行います。CodeBuild には管理者が Terraform で登録した環境変数として識別情報を渡します。これらの値と State は AWS 側で非公開管理し、Git に含めません。

GitHub repository は immutable subject 形式を使用します。apply role は `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:environment:terraform-apply`、plan role は同じ prefix の `:pull_request` subject を信頼します。ID は Git 管理外の tfvars に保持します。OIDC 信頼条件を変更した場合は、Terraform plan で対象が role trust policy の更新だけであることを確認してから apply してください。Environment subject には branch 名が含まれないため、apply Environment の deployment branch を `main` だけに制限してください。

## Apply 前の承認設定

GitHub の Settings → Environments → New environment で、Environment `terraform-apply` を作成し、以下を設定してください。この GitHub 側の管理設定はまだ実施していません。

1. Required reviewers を有効にし、apply を承認するユーザーまたはチームを最低 1 件登録して保存します。複数人を登録しても通常はそのうち 1 人の承認で進むため、全員承認を要求する設定ではありません。
2. Deployment branches and tags を Selected branches and tags にし、ブランチ `main` だけを許可します。全ブランチ許可やタグ許可にしないでください。
3. 管理者も承認を省略できないようにする場合は、管理者による protection rules の bypass を許可しない設定にします。
4. Prevent self-review を有効にする場合、workflow を起動する本人以外の承認者が必要です。承認者を起動者 1 人だけにしないでください。

workflow の `environment` 指定だけでは Required reviewers は設定されません。存在しない Environment が自動作成されたり、保護ルールが未設定だったりすると、承認待ちにならず進む場合があります。Required reviewers と branch 制限を確認してから実行してください。

既存の apply 用 6 Repository Secrets はそのまま使えます。plan 基盤構築後に追加する 3 plan Secrets は Repository Secrets として登録します。Environment Secrets へ移動する必要はありません。同じ名前の Environment Secret を作成した場合は Environment 側が優先されるため、意図せず値を上書きしないでください。

実行順は `inspect` → `static-checks` → Environment 承認待ち → `aws-apply` → `merge` です。Environment 承認前は AWS ジョブの runner とステップは開始されず、OIDC 認証・Pipeline 起動は行いません。却下された場合も apply と自動マージは行いません。承認待ちの間に PR HEAD/base が変わった場合は、承認後の再確認で Pipeline 起動を止めます。

この承認は Pipeline 起動前の許可です。現在は静的解析後に承認する構成で、具体的な Terraform plan 差分を提示して承認する構成ではありません。

今回の承認ゲート変更は基盤変更 PR として手動で main にマージしてください。作業中の `ci-test` の検証用タグ変更を同じ PR に含めず、承認ゲートを反映した後の別 PR にします。AWS の OIDC はすでに Environment subject に切り替えているため、変更を main に反映するまでは古い workflow を起動しないでください。

## PR Plan と手動 Apply

自動 plan は [.github/workflows/terraform-plan.yml](../../../../.github/workflows/terraform-plan.yml)、手動 apply は [.github/workflows/terraform-apply.yml](../../../../.github/workflows/terraform-apply.yml) です。共通補助コードは [.github/scripts/terraform_apply.py](../../../../.github/scripts/terraform_apply.py) で、登録対象は `ci-test` だけです。

1. 管理者が plan 基盤 Terraform を構築し、plan 用 OIDC role ARN、artifact bucket、plan pipeline map の 3 Repository Secrets を追加します。公開 PR の plan は AWS の read-only role だけを使います。
2. `terraform/env/poc/ci-test/` を変更する PR を `main` に作成します。`pull_request_target` の自動 workflow は trusted な base SHA の helper を起動し、PR 本体のコードは AWS 権限なしの fmt/validate job だけで checkout します。fork PR、基盤や workflow の変更、未登録 root は AWS plan に進めません。
3. static checks が成功すると `aws-plan` が OIDC で plan role を引き受け、CodeConnections に PR head SHA を指定して plan Pipeline を起動します。取得した Source commit SHA も CodeBuild が照合します。plan log は GitHub Actions Summary に整形して表示されます。
4. 完了後、head commit に `Terraform plan` status が記録されます。status description に PR base SHA を含め、失敗・timeout・log取得失敗時は failure status にします。
5. apply は手動です。Actions → `Terraform apply PR (manual)` → Run workflow で Branch に `main`、入力 `pr_branch` に PR の source branch 名を指定します。PR 番号の入力は不要です。branch から open・同一 repo・base main の PR を一意に解決し、current head/base と成功 plan status を検証します。
6. static checks の後 `terraform-apply` Environment の承認待ちへ進みます。承認後にだけ AWS apply job が開始され、head/base と成功 plan status を再検証して apply Pipeline を実行します。
7. plan/apply とも CodeBuild の CloudWatch Logs を全ページ取得し、マーカー間ログを Summary に表示します。apply 成功後は PR branch/head/base/成功 plan status を再検証して PR を自動マージします。失敗や log retrieval error はマージされません。

workflow の base を更新・push する PR 自体は apply workflow の対象にしません。workflow change を `main` に反映し、plan 基盤と追加 Secrets を設定してから、次の対象 root PR で確認してください。

workflow 全体をリポジトリ単位で直列化し、実行中の apply を新しい起動でキャンセルしない設定です。GitHub concurrency の待機枠には上限があるため、連続起動時は未実行の待機 run が置き換わる場合があります。Pipeline 側も root ごとの QUEUED モードで直列化しています。

Pipeline の待機上限は 40 分、AWS ジョブの上限は 55 分です。失敗・タイムアウト・ログ取得失敗・実行中の HEAD/base 更新ではマージしません。ただし、すでに行われた AWS 変更はロールバックされず、GitHub の run をキャンセルしても AWS の実行は自動停止しません。AWS の状態を確認してから再実行してください。

GitHub API は HEAD SHA の一致をマージ時に検証しますが、base の確認と merge API は単一トランザクションではありません。base の更新を厳密に制御する運用では、main の変更制御と適切な branch rules も必要です。必須レビュー・必須チェック・許可されたマージ方式は GitHub の設定に従い、拒否された場合は workflow が失敗します。マージ方式には merge commit を使用します。

この公開リポジトリでは Actions Summary も公開されます。既知のバケット名・Pipeline 名・リージョン、ARN、アカウント ID、UUID は表示前にマスキングし、AWS 認証 action でもアカウント ID を伏せます。全生ログの Artifact アップロードは行いません。Secrets の登録だけでログの漏えいが完全に防げるわけではなく、加工・分割された値や未知の識別子までは保証できません。PR の Terraform コード自体もレビューしてください。GitHub の owner 名・リポジトリ名や既存履歴は公開リポジトリの性質上隠せません。

## ローカル検証

リポジトリのルートで、外部 API や Terraform apply を呼ばずに検証できます。

```bash
python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
```

49 テストと actionlint で検証済みです。PR 判定、branch 紐付け、plan status 照合、SHA 更新時の停止、Pipeline 待機、ログのページング、取得失敗時の停止、固定 SHA マージ、識別子マスキングを模擬します。plan 基盤 Terraform は validate と read-only plan で検証済みですが、AWS apply と新 workflow 実行は未実施です。CodeBuild の非公開入力設定は反映済みで、apply infrastructure の plan は OIDC trust role 更新だけです。plan 基盤の構築時はさらに artifact bucket と plan log group が追加されます。