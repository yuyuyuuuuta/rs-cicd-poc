resource "aws_codepipeline" "plan" {
  for_each       = {}
  name           = "${local.name_prefix}-${each.key}-plan"
  role_arn       = aws_iam_role.pipeline[each.key].arn
  pipeline_type  = "V2"
  execution_mode = "SUPERSEDED"
  tags           = local.tags

  lifecycle {
    prevent_destroy = true
  }

  artifact_store {
    location = aws_s3_bucket.artifacts.bucket
    type     = "S3"
  }

  variable {
    name          = "PrCommitSha"
    default_value = ""
    description   = "Expected PR head SHA; plan is rejected if empty or mismatched"
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
        ConnectionArn        = var.codeconnections_arn
        FullRepositoryId     = var.github_repository
        BranchName           = var.github_default_branch
        DetectChanges        = "false"
        OutputArtifactFormat = "CODE_ZIP"
      }
    }
  }

  stage {
    name = "Plan"

    action {
      name            = "TerraformPlan"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["SourceOutput"]

      configuration = {
        ProjectName = aws_codebuild_project.plan[each.key].name
        EnvironmentVariables = jsonencode([
          { name = "EXPECTED_COMMIT_SHA", value = "#{variables.PrCommitSha}", type = "PLAINTEXT" },
          { name = "SOURCE_COMMIT_SHA", value = "#{SourceVariables.CommitId}", type = "PLAINTEXT" }
        ])
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