data "aws_s3_bucket" "ci_test" {
  bucket = aws_s3_bucket.ci_test.id
}