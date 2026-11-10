resource "aws_cloudwatch_log_group" "plan" {
  for_each          = local.targets
  name              = "/aws/codebuild/${local.name_prefix}-${each.key}-plan"
  retention_in_days = 14
  tags              = local.tags
}

resource "aws_codebuild_project" "plan" {
  for_each      = local.targets
  name          = "${local.name_prefix}-${each.key}-plan"
  description   = "Plans only ${each.value.root_dir} at the requested commit"
  service_role  = aws_iam_role.build[each.key].arn
  build_timeout = 30
  tags          = local.tags

  artifacts {
    type = "CODEPIPELINE"
  }

  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
    type                        = "LINUX_CONTAINER"
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "TF_VERSION"
      value = "1.16.6"
    }
    environment_variable {
      name  = "BUILD_MODE"
      value = "plan"
    }
    environment_variable {
      name  = "PLAN_BUCKET"
      value = aws_s3_bucket.artifacts.bucket
    }
    environment_variable {
      name  = "GITHUB_REPOSITORY_NAME"
      value = var.github_repository
    }
    environment_variable {
      name  = "TARGET_STATE_DIR"
      value = each.value.root_dir
    }
    environment_variable {
      name  = "STATE_BUCKET"
      value = local.state_bucket
    }
    environment_variable {
      name  = "STATE_KEY"
      value = "${each.value.root_dir}/terraform.tfstate"
    }
    environment_variable {
      name  = "TF_IN_AUTOMATION"
      value = "true"
    }
    environment_variable {
      name  = "TF_VAR_aws_account_id"
      value = var.aws_account_id
    }
    environment_variable {
      name  = "TF_VAR_aws_region"
      value = var.aws_region
    }
    environment_variable {
      name  = "TF_VAR_test_bucket_name"
      value = each.value.bucket_name
    }
  }

  source {
    type      = "CODEPIPELINE"
    buildspec = replace(file("${path.module}/buildspec/terraform-plan.yml"), "__TRUSTED_RUNNER__", indent(8, file("${path.module}/../infra-cicd-apply/buildspec/terraform_runner.py")))
  }

  logs_config {
    cloudwatch_logs {
      status     = "ENABLED"
      group_name = aws_cloudwatch_log_group.plan[each.key].name
    }
  }

  depends_on = [aws_iam_role_policy.build]
}