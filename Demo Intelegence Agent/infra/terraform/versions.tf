terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # NO BACKEND BLOCK — deliberately, and this is an operator decision that was
  # escalated rather than made silently.
  #
  # The Tutor Intelligence stack uses `backend "s3"`. Demo may not create a new
  # S3 bucket, and reusing Tutor's state bucket would put two independent
  # lifecycles in one blast radius.
  #
  # So state is configured at init time, by whoever runs it:
  #
  #   # An already-approved non-S3 backend (preferred):
  #   terraform init -backend-config=backend.hcl
  #
  #   # Or local state, for a single operator with a documented handover:
  #   terraform init
  #
  # `backend.hcl.example` shows the two shapes that were considered.
  # See docs/operations/terraform-state.md for the decision record.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Service   = "demo-command-center"
      ManagedBy = "terraform"
    }
  }
}
