variable "environment" {
  type        = string
  description = "Deployment environment."
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be dev, staging or production."
  }
}

variable "artifact_bucket" {
  type        = string
  description = "S3 bucket holding immutable Lambda bundles."
}

variable "artifact_key" {
  type        = string
  description = "S3 key of the bundle to deploy, keyed by git SHA."
}

variable "secret_name" {
  type        = string
  description = <<-EOT
    Secrets Manager secret holding every runtime credential. Referenced by name
    so no secret value is ever written into Terraform state.
  EOT
}

variable "db_instance_identifier" {
  description = <<-EOT
    The RDS instance this service connects to, for the connection alarm.

    An instance identifier, not a proxy name: there is no RDS Proxy any more.
    Every Lambda opens its own connection straight to PostgreSQL, so the
    instance's own DatabaseConnections metric is the only place exhaustion
    becomes visible.
  EOT
  type        = string
}

variable "db_username" {
  type        = string
  default     = "tutor_match_meta"
  description = "IAM database user. Owns only this service's schema."
}

variable "match_worker_timeout" {
  type    = number
  default = 60
  validation {
    condition     = var.match_worker_timeout >= 30 && var.match_worker_timeout <= 300
    error_message = "match_worker_timeout must be between 30 and 300 seconds."
  }
}

variable "outbound_worker_timeout" {
  type    = number
  default = 30
}

variable "enrich_worker_timeout" {
  description = <<-EOT
    Timeout for the internet-side enrich worker.

    Must comfortably exceed llm_timeout_seconds x (llm_max_retries + 1), or the
    function is killed mid-call and SQS redelivers a turn whose model spend has
    already been incurred.
  EOT
  type        = number
  default     = 60
  validation {
    condition     = var.enrich_worker_timeout >= 20 && var.enrich_worker_timeout <= 300
    error_message = "enrich_worker_timeout must be between 20 and 300 seconds."
  }
}

variable "enrich_worker_concurrency" {
  description = <<-EOT
    Reserved concurrency for the enrich worker.

    This is the OpenAI spend ceiling, not a database ceiling — the enrich worker
    holds no database grant. Sized independently of match_worker_reserved_concurrency
    for exactly that reason: the two functions are bounded by different resources.
  EOT
  type        = number
  default     = 10
  validation {
    condition     = var.enrich_worker_concurrency >= 1 && var.enrich_worker_concurrency <= 200
    error_message = "enrich_worker_concurrency must be between 1 and 200."
  }
}

variable "match_worker_reserved_concurrency" {
  type        = number
  default     = 20
  description = <<-EOT
    Hard ceiling on concurrent match workers. This bounds database connections
    through RDS Proxy; raising it without raising the proxy's connection limit
    is how the database gets exhausted.
  EOT
  validation {
    condition     = var.match_worker_reserved_concurrency >= 1 && var.match_worker_reserved_concurrency <= 200
    error_message = "reserved concurrency must be between 1 and 200."
  }
}

variable "cache_backend" {
  type        = string
  default     = "postgres"
  description = <<-EOT
    `postgres` = in-process L1 over a shared PostgreSQL L2. `memory` = L1 only,
    for a single-container dev stack.

    There is no `redis` option. ElastiCache is the only always-on, hourly-billed
    component this architecture would have, for a workload that scales to zero
    between messages, and it lives in a VPC — which with no NAT Gateway means
    more networking, not less. The previous validation accepted "redis", which
    the application would then have rejected at boot: a deploy-time failure for
    a value Terraform said was valid.
  EOT
  validation {
    condition     = contains(["memory", "postgres"], var.cache_backend)
    error_message = "cache_backend must be memory or postgres. Redis is not used; see cache/postgres_store.py."
  }
}

variable "ingress_allowed_cidrs" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Source CIDRs permitted to call the ingress API. Empty means open to the
    internet, protected by HMAC alone — acceptable, since every request is
    signature-verified before parsing, but narrowing it to the caller's egress
    range removes the unauthenticated flood entirely.
  EOT
}

variable "api_throttle_burst" {
  type        = number
  default     = 50
  description = "API Gateway burst ceiling. The outermost layer of §8."
}

variable "api_throttle_rate" {
  type        = number
  default     = 20
  description = "API Gateway steady-state requests/second."
}

variable "monthly_budget_usd" {
  type        = number
  default     = 200
  description = "AWS Budgets ceiling for this service's tagged resources."
}

variable "llm_monthly_budget_micros" {
  type        = number
  default     = 50000000 # $50
  description = <<-EOT
    Alarm threshold for model spend, in micro-USD per month, evaluated against
    the LlmCostMicros metric. Separate from the AWS budget because model spend
    is the line item most able to run away in a day, and AWS Budgets alarms are
    too slow to catch that.
  EOT
}

variable "budget_notification_emails" {
  type    = list(string)
  default = []
}

variable "glue_enabled" {
  type        = bool
  default     = true
  description = "Offline catalog + ETL over the sanitised analytics export. Never on the matching path."
}

variable "log_level" {
  type    = string
  default = "INFO"
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "alarm_topic_arns" {
  type        = list(string)
  default     = []
  description = "SNS topics notified on alarm."
}

variable "internal_api_reserved_concurrency" {
  type        = number
  default     = 10
  description = <<-EOT
    Hard ceiling on concurrent internal-API executions. Counts against the same
    RDS Proxy connection budget as the match worker: the alarm in cost.tf is
    keyed on the sum of the two, so raising either without raising the proxy's
    limit is what exhausts the shared database.
  EOT
  validation {
    condition     = var.internal_api_reserved_concurrency >= 1 && var.internal_api_reserved_concurrency <= 100
    error_message = "internal_api_reserved_concurrency must be between 1 and 100."
  }
}

variable "feed_fetch_schedule" {
  description = <<-EOT
    How often the internet-side fetcher pages the website tutor feed.

    Slower than the ingest schedule on purpose: fetching costs a request
    to the website for every page, and applying a staged page costs an S3
    GET. There is no benefit to fetching more often than tutor data
    actually changes.
  EOT
  type        = string
  default     = "rate(1 hour)"
}
