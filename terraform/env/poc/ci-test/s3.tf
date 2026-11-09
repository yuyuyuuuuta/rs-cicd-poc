resource "aws_s3_bucket" "ci_test" {
  bucket        = var.test_bucket_name
  force_destroy = false

  tags = {
    Project     = "rs-cicd-poc"
    Environment = "poc"
    ManagedBy   = "Terraform"
  }
}

resource "aws_s3_bucket_public_access_block" "ci_test" {
  bucket                  = aws_s3_bucket.ci_test.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ci_test" {
  bucket = aws_s3_bucket.ci_test.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}