###############################################################################
# demo-command-center — variables
#
# No cloud price, no model id and no business threshold appears here. Prices
# live in docs/operations/cost-model.md as parameters; model ids and business
# numbers are settings and policy YAML respectively.
###############################################################################

variable "environment" {
  description = "Deployment environment. Part of every resource name."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging or production."
  }
}

variable "aws_region" {
  description = "Region for every Demo resource."
  type        = string
  default     = "ap-south-1"
}

variable "release_sha" {
  description = "Git SHA of the deployed package. Stamped on aliases and logs."
  type        = string
}

###############################################################################
# Package — direct upload, never S3
###############################################################################

variable "lambda_package_path" {
  description = <<-EOT
    Path to the built deployment ZIP. Uploaded directly to Lambda.

    Demo may not create an S3 bucket, so there is no artifact bucket. The build
    script enforces the direct-upload size limit and fails rather than falling
    back to S3.
  EOT
  type        = string
  default     = "../../dist/demo_command_center.zip"
}

###############################################################################
# Existing shared infrastructure — referenced, never created
###############################################################################

variable "persistence_mode" {
  description = <<-EOT
    How the functions reach PostgreSQL. This is an OPERATOR DECISION, not a
    default that can be assumed, because the two modes need different things
    from the database and from the network:

      * `data_api`     — Aurora Serverless RDS Data API over a public AWS
                         endpoint. No VPC, no NAT, no driver, no security
                         group. Requires a cluster with the Data API ENABLED.
      * `postgres_dsn` — a direct asyncpg connection to a normal PostgreSQL
                         endpoint. Works against any instance, including one
                         with no Data API — which is what the shared
                         `TMM_POSTGRES_DSN` currently points at. Requires the
                         instance to be reachable from the function AND its
                         security group to admit that traffic. See
                         docs/final-integration-gaps.md, which records why a
                         function outside a VPC has no stable source address
                         and what the three resolutions cost.

    There is deliberately no default. Picking one silently would either target
    a Data API that is not enabled or open a database to a dynamic address.
  EOT
  type        = string
  validation {
    condition     = contains(["data_api", "postgres_dsn"], var.persistence_mode)
    error_message = "persistence_mode must be data_api or postgres_dsn."
  }
}

variable "aurora_cluster_arn" {
  description = <<-EOT
    ARN of the EXISTING Aurora Serverless cluster. Demo creates no cluster; it
    owns only the `demo_agent` schema inside this one and reaches it over the
    Data API. Empty when `persistence_mode = "postgres_dsn"`.
  EOT
  type        = string
  default     = ""
  validation {
    condition     = var.aurora_cluster_arn == "" || can(regex("^arn:aws:rds:", var.aurora_cluster_arn))
    error_message = "aurora_cluster_arn must be empty or an existing RDS cluster ARN."
  }
}

variable "aurora_secret_arn" {
  description = <<-EOT
    Secrets Manager ARN holding the database credential.

    In `data_api` mode this is the credential the Data API authenticates with.
    In `postgres_dsn` mode it is the secret holding the DSN itself, which is
    why it is required in both: the connection string is a credential and is
    never passed as a plaintext Lambda environment variable.
  EOT
  type        = string
}

variable "aurora_database" {
  description = "Database name inside the existing cluster."
  type        = string
  default     = "nxtutors"
}

variable "secret_name" {
  description = "Secrets Manager secret holding Demo's provider credentials."
  type        = string
}

###############################################################################
# External surfaces
###############################################################################

variable "gateway_base_url" {
  description = "NXTutors website gateway base URL."
  type        = string
  default     = ""
}

variable "website_public_base_url" {
  description = "Public site. The only non-provider host allowed in an outbound link."
  type        = string
  default     = "https://nxtutors.com"
}

###############################################################################
# Timeouts — each queue's visibility timeout is derived from these
###############################################################################

variable "orchestrator_timeout" {
  type    = number
  default = 60
}

variable "scheduling_worker_timeout" {
  description = "Long enough for Google's asynchronous conference creation to settle."
  type        = number
  default     = 90
}

variable "reminder_worker_timeout" {
  type    = number
  default = 60
}

variable "outbound_worker_timeout" {
  type    = number
  default = 30
}

variable "payment_worker_timeout" {
  type    = number
  default = 60
}

variable "analytics_worker_timeout" {
  type    = number
  default = 120
}

###############################################################################
# Reserved concurrency — the priority model, expressed as capacity
#
# Payment gets the smallest pool and the highest priority: it is low volume and
# high consequence, and a wide pool only widens the window in which two workers
# race one order. Analytics gets the largest pool and may be starved freely.
###############################################################################

variable "ingress_reserved_concurrency" {
  description = "Caps the internet-facing function and protects the queues from a flood."
  type        = number
  default     = 50
}

variable "orchestrator_reserved_concurrency" {
  type    = number
  default = 40
}

variable "scheduling_reserved_concurrency" {
  type    = number
  default = 20
}

variable "outbound_reserved_concurrency" {
  description = "Also the ceiling on the Meta send rate."
  type        = number
  default     = 20
}

variable "reminder_reserved_concurrency" {
  type    = number
  default = 10
}

variable "payment_reserved_concurrency" {
  type    = number
  default = 5
}

variable "analytics_reserved_concurrency" {
  description = "Largest pool, lowest priority. Safe to starve."
  type        = number
  default     = 30
}

variable "ops_api_reserved_concurrency" {
  type    = number
  default = 10
}

###############################################################################
# Observability
###############################################################################

variable "log_level" {
  type    = string
  default = "INFO"
  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "log_level must be DEBUG, INFO, WARNING or ERROR."
  }
}

variable "log_retention_days" {
  description = "Finite by requirement. Indefinite retention is a cost and a liability."
  type        = number
  default     = 30
  validation {
    condition     = var.log_retention_days > 0 && var.log_retention_days <= 365
    error_message = "log_retention_days must be between 1 and 365."
  }
}

variable "tracing_mode" {
  description = <<-EOT
    X-Ray mode. `Active` on every function traces everything and costs
    accordingly; `PassThrough` samples only what an upstream already sampled.
    Production defaults to sampled — see docs/operations/cost-model.md.
  EOT
  type        = string
  default     = "PassThrough"
  validation {
    condition     = contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode must be Active or PassThrough."
  }
}

variable "alarm_topic_arns" {
  description = "SNS topics for alarm actions."
  type        = list(string)
  default     = []
}

variable "queue_age_alarm_seconds" {
  description = "Scheduling queue age that means parents are visibly waiting."
  type        = number
  default     = 120
}
