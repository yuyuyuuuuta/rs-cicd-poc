terraform {
  backend "s3" {
    key          = "terraform/env/poc/infra-cicd-plan/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}