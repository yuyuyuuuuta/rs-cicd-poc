resource "aws_iam_role" "pipeline" {
  for_each = local.targets
  name     = "${local.name_prefix}-${each.key}-pipeline-role"
  tags     = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "codepipeline.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "pipeline" {
  for_each = local.targets
  name     = "${local.name_prefix}-${each.key}-pipeline-policy"
  role     = aws_iam_role.pipeline[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["codeconnections:UseConnection"]
        Resource = aws_codestarconnections_connection.github.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketVersioning", "s3:GetBucketLocation", "s3:GetBucketAcl", "s3:ListBucket"]
        Resource = aws_s3_bucket.artifacts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Effect = "Allow"
        Action = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
        Resource = [
          aws_codebuild_project.apply[each.key].arn,
          "arn:aws:codebuild:${var.aws_region}:${var.aws_account_id}:project/${local.name_prefix}-${each.key}-plan"
        ]
      }
    ]
  })
}

resource "aws_iam_role" "build" {
  for_each = local.targets
  name     = "${local.name_prefix}-${each.key}-build-role"
  tags     = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "codebuild.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "build" {
  for_each = local.targets
  name     = "${local.name_prefix}-${each.key}-build-policy"
  role     = aws_iam_role.build[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.apply[each.key].arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetBucketAcl", "s3:ListBucket"]
        Resource = aws_s3_bucket.artifacts.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:GetBucketVersioning"]
        Resource = "arn:aws:s3:::${local.state_bucket}"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${local.state_bucket}"
        Condition = {
          StringLike = { "s3:prefix" = [each.value.root_dir, "${each.value.root_dir}/*"] }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${local.state_bucket}/${each.value.root_dir}/terraform.tfstate",
          "arn:aws:s3:::${local.state_bucket}/${each.value.root_dir}/terraform.tfstate.tflock"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = "arn:aws:s3:::${local.state_bucket}/${each.value.root_dir}/terraform.tfstate.tflock"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = [
          "arn:aws:s3:::${var.plan_artifact_bucket_name}/plans/${var.github_repository}/pr-*/*/${substr(sha256(each.value.root_dir), 0, 16)}/*",
          "arn:aws:s3:::${var.plan_artifact_bucket_name}/approvals/plans/${var.github_repository}/pr-*/*/${substr(sha256(each.value.root_dir), 0, 16)}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = [
          "arn:aws:s3:::${var.plan_artifact_bucket_name}/consumed/plans/${var.github_repository}/pr-*/*/${substr(sha256(each.value.root_dir), 0, 16)}/*",
          "arn:aws:s3:::${var.plan_artifact_bucket_name}/consumed/by-head/${var.github_repository}/pr-*/*/${substr(sha256(each.value.root_dir), 0, 16)}.json",
          "arn:aws:s3:::${var.plan_artifact_bucket_name}/diagnostics/plans/${var.github_repository}/pr-*/*/${substr(sha256(each.value.root_dir), 0, 16)}/*"
        ]
      },
      {
        Effect = "Allow"
        Action = [
          "s3:CreateBucket", "s3:DeleteBucket", "s3:Get*", "s3:ListBucket",
          "s3:PutBucketTagging", "s3:PutBucketPublicAccessBlock", "s3:PutEncryptionConfiguration"
        ]
        Resource = "arn:aws:s3:::${each.value.bucket_name}"
      }
    ]
  })
}

resource "aws_iam_role" "github_actions" {
  name = "${local.name_prefix}-github-actions-apply-role"
  tags = local.tags
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${split("/", var.github_repository)[0]}@${var.github_repository_owner_id}/${split("/", var.github_repository)[1]}@${var.github_repository_id}:environment:terraform-apply"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions" {
  name = "${local.name_prefix}-github-actions-apply-policy"
  role = aws_iam_role.github_actions.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "codepipeline:StartPipelineExecution", "codepipeline:GetPipelineExecution",
          "codepipeline:ListActionExecutions"
        ]
        Resource = [for pipeline in aws_codepipeline.apply : pipeline.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["codebuild:BatchGetBuilds"]
        Resource = [for project in aws_codebuild_project.apply : project.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents"]
        Resource = [for group in aws_cloudwatch_log_group.apply : "${group.arn}:log-stream:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:aws:s3:::${var.plan_artifact_bucket_name}/plans/*/manifest.json"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "arn:aws:s3:::${var.plan_artifact_bucket_name}/approvals/*"
      }
    ]
  })
}