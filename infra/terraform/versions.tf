terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }

  # Configured per environment at init time:
  #   terraform init -backend-config="key=<env>/terraform.tfstate"
  backend "s3" {
    encrypt = true
  }
}

provider "aws" {
  default_tags {
    tags = {
      Service   = "tutor-match-meta"
      ManagedBy = "terraform"
    }
  }
}
