output "pipeline_names" {
  value = { for target, pipeline in aws_codepipeline.apply : local.targets[target].root_dir => pipeline.name }
}

output "github_actions_role_arn" {
  value = aws_iam_role.github_actions.arn
}

output "codeconnections_arn" {
  value = aws_codestarconnections_connection.github.arn
}

output "artifact_bucket_name" {
  value = aws_s3_bucket.artifacts.id
}

output "log_group_names" {
  value = { for target, group in aws_cloudwatch_log_group.apply : target => group.name }
}