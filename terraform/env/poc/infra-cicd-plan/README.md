# Terraform plan 基盤

このディレクトリは plan 用 Terraform root の配置先です。現段階では基盤コードは未実装です。

対象 root ごとに専用 CodePipeline を作成し、CodeConnections で指定した PR コミットを取得します。CodeBuild が固定された対象 root を plan し、GitHub Actions が完了待機・CloudWatch Logs 取得を担当します。

基盤自身は対象 root に含めず、管理者が手動構築します。apply 基盤とは State と実行権限を分けます。