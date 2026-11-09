locals {
  name_prefix  = var.name_prefix
  state_bucket = var.state_bucket_name
  targets = {
    ci-test = {
      root_dir    = "terraform/env/poc/ci-test"
      bucket_name = var.test_bucket_name
    }
  }
  tags = {
    Project     = "rs-cicd-poc"
    Environment = "poc"
    ManagedBy   = "Terraform"
  }
}

data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:aws:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
}