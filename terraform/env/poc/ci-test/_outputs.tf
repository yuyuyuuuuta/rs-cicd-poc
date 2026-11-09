output "test_bucket_name" {
  value = aws_s3_bucket.ci_test.id
}

output "test_bucket_arn" {
  value = data.aws_s3_bucket.ci_test.arn
}