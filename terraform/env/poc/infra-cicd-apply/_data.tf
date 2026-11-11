locals {
  name_prefix        = var.name_prefix
  state_bucket       = var.state_bucket_name
  target_root_parent = "terraform/env/poc"
  target_root_names = toset([
    for file in fileset("${path.module}/..", "*/*.tf") : split("/", file)[0]
    if !startswith(split("/", file)[0], "infra-cicd-")
  ])
  targets = {
    for target in local.target_root_names : target => {
      root_dir    = "${local.target_root_parent}/${target}"
      bucket_name = lookup(var.target_bucket_names, target, var.test_bucket_name)
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