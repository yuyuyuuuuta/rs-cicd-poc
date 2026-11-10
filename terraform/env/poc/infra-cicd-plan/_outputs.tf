output "pipeline_names" {
  value = { for target, pipeline in aws_codepipeline.plan : target => pipeline.name }
}

output "github_plan_role_arn" {
  value = aws_iam_role.github_plan.arn
}

output "artifact_bucket_name" {
  value = aws_s3_bucket.artifacts.id
}

output "log_group_names" {
  value = { for target, group in aws_cloudwatch_log_group.plan : target => group.name }
}