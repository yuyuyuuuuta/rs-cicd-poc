resource "aws_iam_role" "pipeline" {
  for_each = local.targets
  name     = "${local.name_prefix}-${each.key}-plan-pipeline-role"
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
  name     = "${local.name_prefix}-${each.key}-plan-pipeline-policy"
  role     = aws_iam_role.pipeline[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["codeconnections:UseConnection"]
        Resource = var.codeconnections_arn
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
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild", "codebuild:BatchGetBuilds"]
        Resource = aws_codebuild_project.plan[each.key].arn
      }
    ]
  })
}

resource "aws_iam_role" "build" {
  for_each = local.targets
  name     = "${local.name_prefix}-${each.key}-plan-build-role"
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
  name     = "${local.name_prefix}-${each.key}-plan-build-policy"
  role     = aws_iam_role.build[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.plan[each.key].arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:GetBucketVersioning", "s3:GetBucketAcl"]
        Resource = [aws_s3_bucket.artifacts.arn, "arn:aws:s3:::${local.state_bucket}"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${local.state_bucket}"
        Condition = {
          StringLike = {
            "s3:prefix" = ["${each.value.root_dir}", "${each.value.root_dir}/*"]
          }
        }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "arn:aws:s3:::${local.state_bucket}/${each.value.root_dir}/terraform.tfstate"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${local.state_bucket}/${each.value.root_dir}/terraform.tfstate.tflock"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:Get*", "s3:ListBucket"]
        Resource = "arn:aws:s3:::${each.value.bucket_name}"
      },
      {
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_role" "github_plan" {
  name = "${local.name_prefix}-github-actions-plan-role"
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
          "token.actions.githubusercontent.com:sub" = "repo:${split("/", var.github_repository)[0]}@${var.github_repository_owner_id}/${split("/", var.github_repository)[1]}@${var.github_repository_id}:ref:refs/heads/${var.github_default_branch}"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_plan" {
  name = "${local.name_prefix}-github-actions-plan-policy"
  role = aws_iam_role.github_plan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["codepipeline:StartPipelineExecution", "codepipeline:GetPipelineExecution", "codepipeline:ListActionExecutions"]
        Resource = [for pipeline in aws_codepipeline.plan : pipeline.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["codebuild:BatchGetBuilds"]
        Resource = [for project in aws_codebuild_project.plan : project.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents"]
        Resource = [for group in aws_cloudwatch_log_group.plan : "${group.arn}:log-stream:*"]
      }
    ]
  })
}