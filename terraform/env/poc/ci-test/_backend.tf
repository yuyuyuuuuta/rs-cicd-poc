terraform {
  backend "s3" {
    key          = "terraform/env/poc/ci-test/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}