resource "aws_codepipeline" "apply" {
  for_each       = local.targets
  name           = "${local.name_prefix}-${each.key}-apply"
  role_arn       = aws_iam_role.pipeline[each.key].arn
  pipeline_type  = "V2"
  execution_mode = "QUEUED"
  tags           = local.tags

  artifact_store {
    location = aws_s3_bucket.artifacts.bucket
    type     = "S3"
  }

  variable {
    name          = "PrCommitSha"
    default_value = ""
    description   = "Expected PR head SHA; apply is rejected if empty or mismatched"
  }

  dynamic "variable" {
    for_each = toset(["ExecutionMode", "PrNumber", "BaseCommitSha", "PlanRunId", "PlanAttempt", "ManifestVersion"])
    content {
      name          = variable.value
      default_value = "UNSET"
    }
  }

  stage {
    name = "Source"

    action {
      name             = "Source"
      namespace        = "SourceVariables"
      category         = "Source"
      owner            = "AWS"
      provider         = "CodeStarSourceConnection"
      version          = "1"
      output_artifacts = ["SourceOutput"]

      configuration = {
        ConnectionArn        = aws_codestarconnections_connection.github.arn
        FullRepositoryId     = var.github_repository
        BranchName           = var.github_default_branch
        DetectChanges        = "false"
        OutputArtifactFormat = "CODE_ZIP"
      }
    }
  }

  dynamic "stage" {
    for_each = ["plan", "apply"]
    content {
      name = title(stage.value)

      before_entry {
        condition {
          result = "SKIP"
          rule {
            name = "ExecutionMode"
            rule_type_id {
              category = "Rule"
              owner    = "AWS"
              provider = "VariableCheck"
              version  = "1"
            }
            configuration = {
              Variable = "#{variables.ExecutionMode}"
              Value    = stage.value
              Operator = "EQ"
            }
          }
        }
      }

      action {
        name            = "Terraform${title(stage.value)}"
        category        = "Build"
        owner           = "AWS"
        provider        = "CodeBuild"
        version         = "1"
        input_artifacts = ["SourceOutput"]

        configuration = {
          ProjectName = stage.value == "apply" ? aws_codebuild_project.apply[each.key].name : "${local.name_prefix}-${each.key}-plan"
          EnvironmentVariables = jsonencode([
            { name = "EXPECTED_COMMIT_SHA", value = "#{variables.PrCommitSha}", type = "PLAINTEXT" },
            { name = "SOURCE_COMMIT_SHA", value = "#{SourceVariables.CommitId}", type = "PLAINTEXT" },
            { name = "EXECUTION_MODE", value = "#{variables.ExecutionMode}", type = "PLAINTEXT" },
            { name = "PR_NUMBER", value = "#{variables.PrNumber}", type = "PLAINTEXT" },
            { name = "BASE_COMMIT_SHA", value = "#{variables.BaseCommitSha}", type = "PLAINTEXT" },
            { name = "PLAN_RUN_ID", value = "#{variables.PlanRunId}", type = "PLAINTEXT" },
            { name = "PLAN_ATTEMPT", value = "#{variables.PlanAttempt}", type = "PLAINTEXT" },
            { name = "MANIFEST_VERSION", value = "#{variables.ManifestVersion}", type = "PLAINTEXT" },
            { name = "PIPELINE_EXECUTION_ID", value = "#{codepipeline.PipelineExecutionId}", type = "PLAINTEXT" }
          ])
        }
      }
    }
  }

  depends_on = [
    aws_iam_role_policy.pipeline,
    aws_s3_bucket_public_access_block.artifacts,
    aws_s3_bucket_server_side_encryption_configuration.artifacts,
    aws_s3_bucket_versioning.artifacts
  ]
}