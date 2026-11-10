resource "aws_s3_bucket" "artifacts" {
  bucket        = var.artifact_bucket_name
  force_destroy = false
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "ApprovalWriter"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.artifacts.arn}/approvals/*"
        Condition = { ArnNotEquals = { "aws:PrincipalArn" = "arn:aws:iam::${var.aws_account_id}:role/${local.name_prefix}-github-actions-apply-role" } }
      },
      {
        Sid       = "RequireExclusiveClaims"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = ["${aws_s3_bucket.artifacts.arn}/approvals/*", "${aws_s3_bucket.artifacts.arn}/consumed/*"]
        Condition = { StringNotEquals = { "s3:if-none-match" = "*" } }
      }
    ]
  })
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  dynamic "rule" {
    for_each = { plans = 2, approvals = 2, diagnostics = 7 }
    content {
      id     = "expire-${rule.key}"
      status = "Enabled"
      filter {
        prefix = "${rule.key}/"
      }
      expiration {
        days = rule.value
      }
      noncurrent_version_expiration {
        noncurrent_days = rule.value
      }
      abort_incomplete_multipart_upload {
        days_after_initiation = 1
      }
    }
  }
  depends_on = [aws_s3_bucket_versioning.artifacts]
}