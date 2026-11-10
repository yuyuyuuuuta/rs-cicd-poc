# Terraform apply 基盤

`ci-test` の既存 Apply 基盤を、Root ごとに一つの Plan/Apply 共通 Pipeline として再利用します。2026-11-10 に共通化・保存 Plan・S3 保護設定を AWS へ反映し、反映後 Plan の差分なしを確認しました。既存 State、リソースアドレス、Pipeline 名、Connection は維持しています。

Connection、成果物 S3 と付随設定、3 つの IAM ロール、ロググループ、CodeBuild、CodePipeline、3 つのインラインポリシーは作成済みです。今後の変更時も既存 State を維持し、古い保存 plan を再利用せず plan を取り直してください。

## 実装内容

- Pipeline: `TF_PIPELINE_NAMES` Secret で指定します。Source → Plan → Apply とし、`ExecutionMode=plan|apply` に一致するステージだけを実行します。
- Source: CodeConnections、非公開 tfvars の `github_repository`、通常ブランチ `main`、変更検知による自動起動なし。
- 対象: `terraform/env/poc/ci-test` に固定し、基盤 root は対象に含めません。
- 実行: V2 Pipeline の QUEUED モードで、同じ root の実行を直列化します。
- 権限: 専用 CodeBuild ロールはテスト用バケットと対象 State キーだけを書き換えます。管理者権限は付与しません。
- ログ: 実ロググループ名は管理者の `terraform output` で確認します。保存期間 14 日。
- GitHub 用ロール: 承認前は Plan 用 OIDC ロール、Environment 承認後だけ Apply 用ロールを使用します。Apply 用には保存 Plan メタデータの読み取りと承認オブジェクトの排他作成を許可します。

対象 root の追加は `_data.tf` の `local.targets` に登録します。各対象に専用 Pipeline・CodeBuild・サービスロール・ロググループを作ります。対象リソースが S3 以外の場合、専用 CodeBuild ポリシーと buildspec のパス規則も見直してください。

## 実行時の安全条件

起動する側が Source アクションに `COMMIT_ID` の revision override を指定し、Pipeline 変数 `PrCommitSha` に同じ PR HEAD SHA を渡します。CodeBuild は Source アクションが返した `CommitId` と照合します。未指定・不一致は Terraform 実行前に失敗します。

buildspec は管理者が Terraform で CodeBuild に登録するインライン設定です。PR 内の buildspec 変更だけでは実行手順を変更できません。変更を反映するには管理者の基盤 apply が必要です。

Terraform に登録した信頼済み Python を buildspec に埋め込み、`TF_RESULT_JSON` の構造化レコードを CloudWatch Logs へ出します。Actions は件数、リソースの操作種別、変更属性名を Markdown に整形します。値・Outputs・生 Plan は公開しません。生診断は非公開 S3 の `diagnostics/` に保存します。実環境での一連の動作は未検証です。

Plan は S3 の特定 VersionId と SHA-256、PR/head/base/Root/Workflow run/attempt に固定し、有効期間は 24 時間です。承認後は `terraform apply <saved-plan>` だけを実行し、再 Plan しません。Terraform 自身の State ロック・古い Plan の拒否も有効です。S3 の `approvals/` は承認後の OIDC ロールだけが書け、CodeBuild は `consumed/` を条件付き作成して許可を一度だけ消費します。モード変数だけで Apply を認可しません。

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

共通の 8 Secrets を Settings → Secrets and variables → Actions → New repository secret に設定してください。Git 管理外の既存設定は自動更新していないため、[設定テンプレート](../../../../admin/github-secrets.local.json.example) と照合してください。識別情報や認証情報をチャットへ貼り付ける必要はありません。

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

`TF_PIPELINE_NAMES` の実 Pipeline 名は既存 Apply 基盤の `terraform output -json pipeline_names` で確認します。出力のキー `ci-test` は Secret では実パス `terraform/env/poc/ci-test` に対応付けます。Plan もこの対応表を使い、旧 `TF_PLAN_PIPELINE_NAMES` は参照しません。

長期 AWS アクセスキー、Secret Access Key、セッショントークンは登録しません。認証は OIDC で行います。GitHub 操作用の `GITHUB_TOKEN` は Actions 実行時に自動提供されます。リポジトリの設定・組織ポリシーで、使用 action とマージ専用ジョブの書き込み権限が許可されている必要があります。

静的検証ジョブには識別情報 Secrets を渡しません。AWS ジョブの trusted なコードだけが設定確認と起動・ログ取得を行います。CodeBuild には管理者が Terraform で登録した環境変数として識別情報を渡します。これらの値と State は AWS 側で非公開管理し、Git に含めません。

GitHub repository は immutable subject 形式を使用します。Apply role は `repo:<OWNER>@<OWNER_ID>/<REPOSITORY>@<REPOSITORY_ID>:environment:terraform-apply`、Plan role は同じ prefix の `:ref:refs/heads/main` を信頼します。ID は非公開 tfvars に保持します。OIDC subject の実設定との一致を確認してください。Environment subject に branch 名は含まれないため、deployment branch を `main` に制限します。

## Apply 前の承認設定

GitHub の Settings → Environments → New environment で、Environment `terraform-apply` を作成し、以下を設定してください。この GitHub 側の管理設定はまだ実施していません。

1. Required reviewers を有効にし、apply を承認するユーザーまたはチームを最低 1 件登録して保存します。複数人を登録しても通常はそのうち 1 人の承認で進むため、全員承認を要求する設定ではありません。
2. Deployment branches and tags を Selected branches and tags にし、ブランチ `main` だけを許可します。全ブランチ許可やタグ許可にしないでください。
3. 管理者も承認を省略できないようにする場合は、管理者による protection rules の bypass を許可しない設定にします。
4. Prevent self-review を有効にする場合、workflow を起動する本人以外の承認者が必要です。承認者を起動者 1 人だけにしないでください。

workflow の `environment` 指定だけでは Required reviewers は設定されません。存在しない Environment が自動作成されたり、保護ルールが未設定だったりすると、承認待ちにならず進む場合があります。Required reviewers と branch 制限を確認してから実行してください。

既存の共通 Secrets に加え、Plan 用 OIDC role と artifact bucket の 2 Secrets が必要です。同名 Environment Secret は Repository Secret より優先されるため、意図せず上書きしないでください。

実行順は `inspect` → `static-checks` → `aws-replan` → Environment 承認待ち → `aws-apply` → `merge` です。承認前に read-only ロールで最新 Plan を保存し、Summary に差分を表示します。却下時は Apply/Merge しません。承認後にも PR・Plan の対応と期限を再確認します。

承認対象は `aws-replan` が作成した特定の Plan です。承認後に差分を作り直しません。Environment 未設定のまま実行すると承認ゲートにならないため、Required reviewers、自己承認防止、管理者バイパス禁止を実環境で確認してください。

今回の承認ゲート変更は基盤変更 PR として手動で main にマージしてください。作業中の `ci-test` の検証用タグ変更を同じ PR に含めず、承認ゲートを反映した後の別 PR にします。AWS の OIDC はすでに Environment subject に切り替えているため、変更を main に反映するまでは古い workflow を起動しないでください。

## PR Plan とラベル Apply

自動 Plan は [.github/workflows/terraform-plan.yml](../../../../.github/workflows/terraform-plan.yml)、ラベル Apply は [.github/workflows/terraform-apply.yml](../../../../.github/workflows/terraform-apply.yml) です。共通処理は [.github/scripts/terraform_apply.py](../../../../.github/scripts/terraform_apply.py) にあります。登録 Root は現在 `ci-test` だけです。

1. 管理者が以下の反映準備を完了し、Workflow を信頼済み `main` に配置します。基盤変更 PR 自身は通常の Apply 対象にしません。
2. 対象 Root の PR を作成・更新すると、信頼済み base SHA の Workflow が検出と静的解析を行い、共通 Pipeline を Plan モードで起動します。AWS 権限を持つ Actions ジョブは PR 内のスクリプトを実行しません。
3. Source revision override と CodeBuild の照合で PR head SHA を固定します。件数・差分を Summary へ掲載し、head/base に結び付いた成功 status と実行記録を残します。
4. write/maintain/admin 権限の担当者が `apply-request` を付けます。PR はイベントから特定し、再実行者の権限も確認します。成功 status の URL が指す信頼済み Plan Workflow の成功と、S3 の PR/head/base/Root 記録を照合します。
5. 静的解析後、同じ Pipeline の Plan モードで最新 Plan を保存し、Apply Workflow の Summary に表示します。この Plan の VersionId・ハッシュが承認対象です。
6. Environment 承認後のジョブが承認レコードを排他発行し、同じ Pipeline を Apply モードで起動します。AWS 側でも PR と Workflow を再確認し、保存 Plan のみを適用します。GitHub API が読めない場合は fail closed です。現在の CodeBuild 側確認は公開リポジトリを前提とします。
7. 全 Root の結果を収集し、一つでも失敗すればマージしません。成功時は PR head/base、Plan status、`mergeable_state=clean` と GitHub の保護条件を確認し、head SHA 指定の通常 merge API を使います。拒否理由は Summary に掲載します。

## 今回の反映準備

2026-11-10: 管理者 IAM 更新後、State の TLS 必須ポリシー追加、Plan 用 CodeBuild/IAM/S3 保護、既存 Apply Pipeline の共通化を反映しました。共通 Pipeline の AWS 定義を確認した後、承認済みの旧 Plan 専用 Pipeline 1本だけを削除しました。既存のバケット・CodeBuild・Connection・State と外部付与の `Owner` タグは保持しています。非公開 tfvars の相互参照は保存済みで、反映後 Plan は差分なしです。削除保護は戻しています。

次は GitHub の8 Secrets・Environment・ブランチ保護を確認し、今回のコードを main に反映します。その後、対象 Root だけを変更する検証 PR で自動 Plan → `apply-request` → 再 Plan → Environment 承認 → 保存 Plan 適用 → 自動マージを確認します。今回の作業では Commit/Push、実 PR の Pipeline 起動、対象環境への Apply は実行していません。

1. AWS 操作の前に、管理者が実 State と変更計画を確認します。旧 Plan 専用 Pipeline が State に存在すると `prevent_destroy` が停止させます。自動削除せず、別途承認した廃止・移行を実施してください。
2. 非公開 tfvars へ、Apply 側に `plan_artifact_bucket_name`、Plan 側に `source_artifact_bucket_name` を追加します。両基盤で同じ既存 `name_prefix` を使います。異なる場合は Plan 側の承認書き手 ARN も実ロールに合わせてレビューします。
3. Plan CodeBuild 名は既存プレフィックスから求めます。異なる場合は Apply 側 `plan_project_names`、Plan 側 `shared_pipeline_names` の map を設定します。
4. 公開 IAM テンプレートの S3 policy/lifecycle 管理権限を非公開の管理者ポリシーへ反映します。今回の既存実 JSON は追加済みで、AWS 側は管理者が更新しました。他用途の IAM/PassRole 権限は維持してください。
5. 別途許可のうえ、State 保護設定、Plan 基盤、既存 Apply 基盤の順に新しい変更計画を確認して反映します。削除・再作成や意図しない変更があれば停止します。古い保存 plan は再利用しません。
6. 8 Secrets、Environment、ブランチ保護を確認します。必須チェックに Apply Workflow 全体の完了を要求するとマージジョブ自身と循環するため、マージ前に完了するチェックを選びます。管理者バイパスは使用しません。

workflow の base を更新・push する PR 自体は apply workflow の対象にしません。workflow change を `main` に反映し、plan 基盤と追加 Secrets を設定してから、次の対象 root PR で確認してください。

PR ごとの Workflow concurrency は PR 更新・ラベル解除・PR 終了で古い承認待ちをキャンセルします。承認後の AWS ジョブはリポジトリ単位で直列化し、Pipeline も Root ごとの QUEUED で直列化します。GitHub の待機枠には上限があるため、待機 run が置き換わる場合があります。AWS 実行がすでに始まっていれば GitHub のキャンセルでは自動停止しません。

適用許可は Plan ごとに一度、同じ PR/head/Root の適用試行も一度だけ消費できます。失敗時も記録を残すため、部分適用・初期化失敗後にラベルを付け直しても無条件では再適用しません。管理者が State、稼働リソース、`consumed/` の対応を確認し、必要なら承認された復旧手順で対象記録を解除してから新しい Plan と承認を取得します。異なる head でも同一 Root の変更は Terraform の古い Plan 検出で拒否されることがあります。

承認待ちが 24 時間を超えた場合や、成功済みジョブだけを再利用する部分 rerun は拒否します。PR Plan を最新にし、ラベルを付け直して全体を再実行してください。登録 Root と共通モジュールの依存関係は Python の `TARGETS` / `DEPENDENCIES` で管理し、共通モジュールの依存も推移的に登録します。未登録パス・基盤変更・fork は引き続き拒否します。

Pipeline の待機上限は 40 分、AWS ジョブの上限は 55 分です。失敗・タイムアウト・ログ取得失敗・実行中の HEAD/base 更新ではマージしません。ただし、すでに行われた AWS 変更はロールバックされず、GitHub の run をキャンセルしても AWS の実行は自動停止しません。AWS の状態を確認してから再実行してください。

GitHub API は HEAD SHA の一致をマージ時に検証しますが、base の確認と merge API は単一トランザクションではありません。base の更新を厳密に制御する運用では、main の変更制御と適切な branch rules も必要です。必須レビュー・必須チェック・許可されたマージ方式は GitHub の設定に従い、拒否された場合は workflow が失敗します。マージ方式には merge commit を使用します。

この公開リポジトリでは Actions Summary も公開されます。既知のバケット名・Pipeline 名・リージョン、ARN、アカウント ID、UUID は表示前にマスキングし、AWS 認証 action でもアカウント ID を伏せます。全生ログの Artifact アップロードは行いません。Secrets の登録だけでログの漏えいが完全に防げるわけではなく、加工・分割された値や未知の識別子までは保証できません。PR の Terraform コード自体もレビューしてください。GitHub の owner 名・リポジトリ名や既存履歴は公開リポジトリの性質上隠せません。

## ローカル検証

リポジトリのルートで、外部 API や Terraform apply を呼ばずに検証できます。

```bash
python3 -m unittest discover -s .github/scripts -p 'test_*.py' -v
```

単体テスト、actionlint、Terraform fmt/validate、埋め込み buildspec の構文をローカル検証します。AWS 基盤への反映と反映後 Plan の差分なしは確認済みです。Environment 設定確認、Actions からの OIDC 実通信、実 PR の Plan/Apply/Merge は未実施です。GitHub Secrets は変更していないため管理者が照合してください。