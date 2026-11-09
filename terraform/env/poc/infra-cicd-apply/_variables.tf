variable "aws_account_id" {
  type = string

  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must contain exactly 12 digits."
  }
}

variable "aws_region" {
  type = string
}

variable "github_repository" {
  type = string
}

variable "github_default_branch" {
  type    = string
  default = "main"
}

variable "name_prefix" {
  type = string
}

variable "state_bucket_name" {
  type = string
}

variable "artifact_bucket_name" {
  type = string
}

variable "test_bucket_name" {
  type = string
}