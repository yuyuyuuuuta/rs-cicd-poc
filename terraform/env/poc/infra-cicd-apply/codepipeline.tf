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

  stage {
    name = "Apply"

    action {
      name            = "TerraformApply"
      category        = "Build"
      owner           = "AWS"
      provider        = "CodeBuild"
      version         = "1"
      input_artifacts = ["SourceOutput"]

      configuration = {
        ProjectName = aws_codebuild_project.apply[each.key].name
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