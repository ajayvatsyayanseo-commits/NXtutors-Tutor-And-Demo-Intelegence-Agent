###############################################################################
# demo-command-center — Lambda functions
#
# One package, many functions. Separate packages would triple the build and
# guarantee version skew between workers that exchange typed events.
#
# TWO CHOICES THAT DIFFER FROM THE TUTOR STACK, BOTH DELIBERATE:
#
#   * `filename` + `source_code_hash`, not `s3_bucket`/`s3_key`. Demo may not
#     create an S3 bucket, so the ZIP is uploaded directly. `scripts/build_lambda.py`
#     enforces the direct-upload size limit at build time and FAILS rather than
#     silently falling back to S3.
#
#   * NO `vpc_config` on any function. Every dependency — the Aurora Data API,
#     SQS, Secrets Manager, Meta, Google, Cashfree, OpenAI — is reachable over
#     a public endpoint, so nothing needs to be inside the VPC, which is
#     precisely what removes the need for a NAT Gateway.
#
#     Caveat, recorded rather than papered over: with
#     `persistence_mode = "postgres_dsn"` the database is reached by a direct
#     connection instead, and a function outside a VPC has no stable source
#     address for a security group to allow. docs/final-integration-gaps.md
#     states the three resolutions and what each costs. Do not widen the
#     database security group to 0.0.0.0/0 to make this mode work.
#
# Every function is published as an immutable version behind the `live` alias.
# Deployment moves the alias; rollback moves it back.
###############################################################################

locals {
  common_environment = {
    DCC_ENVIRONMENT        = var.environment
    DCC_AWS_REGION         = data.aws_region.current.name
    DCC_LOG_LEVEL          = var.log_level
    DCC_SECRETS_PREFIX     = "/${local.name}/"
    DCC_PERSISTENCE_MODE   = var.persistence_mode
    DCC_AURORA_CLUSTER_ARN = var.aurora_cluster_arn
    DCC_AURORA_SECRET_ARN  = var.aurora_secret_arn
    DCC_AURORA_DATABASE    = var.aurora_database
    # Demo's schema, never `tutor_match`. The Tutor half owns that one and this
    # stack has no grant to write it.
    DCC_AURORA_SCHEMA           = "demo_agent"
    DCC_WORK_QUEUE_URL          = aws_sqs_queue.scheduling.url
    DCC_OUTBOUND_QUEUE_URL      = aws_sqs_queue.outbound.url
    DCC_PAYMENT_QUEUE_URL       = aws_sqs_queue.payment.url
    DCC_REMINDER_QUEUE_URL      = aws_sqs_queue.reminders.url
    DCC_ANALYTICS_QUEUE_URL     = aws_sqs_queue.analytics.url
    DCC_SCHEDULER_GROUP_NAME    = aws_scheduler_schedule_group.reminders.name
    DCC_SCHEDULER_ROLE_ARN      = aws_iam_role.scheduler.arn
    DCC_POLICY_DIR              = "config/policies"
    DCC_WEBSITE_PUBLIC_BASE_URL = var.website_public_base_url
    DCC_GATEWAY_BASE_URL        = var.gateway_base_url
    DCC_GIT_SHA                 = var.release_sha
  }

  package      = var.lambda_package_path
  package_hash = filebase64sha256(var.lambda_package_path)
  runtime      = "python3.12"
  # arm64: cheaper per millisecond, and every dependency is pure Python.
  architectures = ["arm64"]
}

###############################################################################
# Ingress — the only publicly reachable function.
#
# Verifies a signature over raw bytes, dedupes, enqueues, returns 200. No LLM,
# no database, no provider call. Short timeout on purpose: if it needs more,
# something has leaked onto the hot path.
###############################################################################

resource "aws_lambda_function" "ingress" {
  function_name = "${local.name}-ingress"
  role          = aws_iam_role.function["ingress"].arn
  handler       = "demo_command_center.handlers.webhooks.meta_webhook"
  runtime       = local.runtime
  architectures = local.architectures
  publish       = true

  filename         = local.package
  source_code_hash = local.package_hash

  timeout     = 10
  memory_size = 512

  # Caps the internet-facing blast radius and protects the queue from a flood.
  reserved_concurrent_executions = var.ingress_reserved_concurrency

  environment { variables = merge(local.common_environment, { DCC_META_ENABLED = "true" }) }
  tracing_config { mode = var.tracing_mode }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

resource "aws_lambda_function" "cashfree_webhook" {
  function_name = "${local.name}-cashfree-webhook"
  role          = aws_iam_role.function["ingress"].arn
  handler       = "demo_command_center.handlers.webhooks.cashfree_webhook"
  runtime       = local.runtime
  architectures = local.architectures
  publish       = true

  filename         = local.package
  source_code_hash = local.package_hash

  timeout     = 15
  memory_size = 512

  reserved_concurrent_executions = var.ingress_reserved_concurrency

  environment { variables = merge(local.common_environment, { DCC_CASHFREE_ENABLED = "true" }) }
  tracing_config { mode = var.tracing_mode }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

###############################################################################
# Workers
###############################################################################

locals {
  workers = {
    orchestrator = {
      handler     = "demo_command_center.handlers.workers.work_queue"
      role        = "orchestrator"
      timeout     = var.orchestrator_timeout
      memory      = 1024
      concurrency = var.orchestrator_reserved_concurrency
      extra       = {}
    }
    scheduling-worker = {
      handler     = "demo_command_center.handlers.capabilities.demo_scheduling_worker"
      role        = "scheduling"
      timeout     = var.scheduling_worker_timeout
      memory      = 1024
      concurrency = var.scheduling_reserved_concurrency
      extra       = { DCC_GOOGLE_ENABLED = "true" }
    }
    reminder-worker = {
      handler     = "demo_command_center.handlers.workers.reminder_sweep"
      role        = "reminder"
      timeout     = var.reminder_worker_timeout
      memory      = 512
      concurrency = var.reminder_reserved_concurrency
      extra       = {}
    }
    outbound-worker = {
      handler     = "demo_command_center.handlers.workers.outbox_relay"
      role        = "outbound"
      timeout     = var.outbound_worker_timeout
      memory      = 512
      concurrency = var.outbound_reserved_concurrency
      extra       = { DCC_META_ENABLED = "true" }
    }
    payment-worker = {
      handler = "demo_command_center.handlers.capabilities.demo_paid_transition_worker"
      role    = "payment"
      timeout = var.payment_worker_timeout
      memory  = 1024
      # Deliberately the smallest pool in the system. Payment work is low
      # volume and high consequence; a wide pool buys nothing and widens the
      # window in which two workers race the same order.
      concurrency = var.payment_reserved_concurrency
      extra       = { DCC_CASHFREE_ENABLED = "true" }
    }
    forecast-worker = {
      handler     = "demo_command_center.handlers.capabilities.demo_forecast_worker"
      role        = "analytics"
      timeout     = var.analytics_worker_timeout
      memory      = 512
      concurrency = var.analytics_reserved_concurrency
      extra       = {}
    }
    objection-worker = {
      handler     = "demo_command_center.handlers.capabilities.demo_objection_worker"
      role        = "analytics"
      timeout     = var.analytics_worker_timeout
      memory      = 1024
      concurrency = var.analytics_reserved_concurrency
      extra       = { DCC_LLM_PROVIDER = "openai" }
    }
    conversion-worker = {
      handler     = "demo_command_center.handlers.capabilities.demo_conversion_worker"
      role        = "analytics"
      timeout     = var.analytics_worker_timeout
      memory      = 512
      concurrency = var.analytics_reserved_concurrency
      extra       = {}
    }
    discount-worker = {
      handler     = "demo_command_center.handlers.capabilities.demo_discount_worker"
      role        = "analytics"
      timeout     = var.analytics_worker_timeout
      memory      = 512
      concurrency = var.analytics_reserved_concurrency
      extra       = {}
    }
    monitoring-worker = {
      handler     = "demo_command_center.handlers.capabilities.demo_monitoring_worker"
      role        = "monitoring"
      timeout     = var.analytics_worker_timeout
      memory      = 1024
      concurrency = var.analytics_reserved_concurrency
      extra       = {}
    }
    ops-api = {
      handler     = "demo_command_center.handlers.workers.internal_handoff"
      role        = "ops-api"
      timeout     = 15
      memory      = 512
      concurrency = var.ops_api_reserved_concurrency
      extra       = {}
    }
  }
}

resource "aws_lambda_function" "worker" {
  for_each = local.workers

  function_name = "${local.name}-${each.key}"
  role          = aws_iam_role.function[each.value.role].arn
  handler       = each.value.handler
  runtime       = local.runtime
  architectures = local.architectures
  publish       = true

  filename         = local.package
  source_code_hash = local.package_hash

  timeout     = each.value.timeout
  memory_size = each.value.memory

  reserved_concurrent_executions = each.value.concurrency

  environment { variables = merge(local.common_environment, each.value.extra) }
  tracing_config { mode = var.tracing_mode }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

###############################################################################
# Aliases — deployment moves `live`; rollback moves it back.
###############################################################################

resource "aws_lambda_alias" "worker" {
  for_each = local.workers

  name             = "live"
  function_name    = aws_lambda_function.worker[each.key].function_name
  function_version = aws_lambda_function.worker[each.key].version
  description      = "release ${var.release_sha}"
}

resource "aws_lambda_alias" "ingress" {
  name             = "live"
  function_name    = aws_lambda_function.ingress.function_name
  function_version = aws_lambda_function.ingress.version
  description      = "release ${var.release_sha}"
}

resource "aws_lambda_alias" "cashfree_webhook" {
  name             = "live"
  function_name    = aws_lambda_function.cashfree_webhook.function_name
  function_version = aws_lambda_function.cashfree_webhook.version
  description      = "release ${var.release_sha}"
}

###############################################################################
# Event source mappings — one lane each, with per-lane concurrency
###############################################################################

resource "aws_lambda_event_source_mapping" "scheduling" {
  event_source_arn = aws_sqs_queue.scheduling.arn
  function_name    = aws_lambda_alias.worker["orchestrator"].arn

  # One at a time. A batch of ten turns in one invocation would serialise
  # unrelated conversations behind each other.
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config { maximum_concurrency = var.orchestrator_reserved_concurrency }
}

resource "aws_lambda_event_source_mapping" "payment" {
  event_source_arn = aws_sqs_queue.payment.arn
  function_name    = aws_lambda_alias.worker["payment-worker"].arn

  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config { maximum_concurrency = var.payment_reserved_concurrency }
}

resource "aws_lambda_event_source_mapping" "outbound" {
  event_source_arn = aws_sqs_queue.outbound.arn
  function_name    = aws_lambda_alias.worker["outbound-worker"].arn

  # Batched: sends are independent, and the per-message idempotency claim makes
  # a partial batch failure safe to redeliver.
  batch_size                         = 5
  maximum_batching_window_in_seconds = 2
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config { maximum_concurrency = var.outbound_reserved_concurrency }
}

resource "aws_lambda_event_source_mapping" "reminders" {
  event_source_arn = aws_sqs_queue.reminders.arn
  function_name    = aws_lambda_alias.worker["reminder-worker"].arn

  batch_size                         = 10
  maximum_batching_window_in_seconds = 5
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config { maximum_concurrency = var.reminder_reserved_concurrency }
}

resource "aws_lambda_event_source_mapping" "analytics" {
  event_source_arn = aws_sqs_queue.analytics.arn
  function_name    = aws_lambda_alias.worker["forecast-worker"].arn

  batch_size                         = 10
  maximum_batching_window_in_seconds = 10
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config { maximum_concurrency = var.analytics_reserved_concurrency }
}

###############################################################################
# Public ingress — Function URLs, not API Gateway.
#
# The webhook needs exactly one route with no auth of its own (the signature IS
# the auth), so an HTTP API would add a hop, a cost and a second place for the
# route to be wrong. `authorization_type = NONE` is correct and deliberate:
# both handlers verify an HMAC over the raw body before anything else.
###############################################################################

resource "aws_lambda_function_url" "meta" {
  function_name      = aws_lambda_function.ingress.function_name
  qualifier          = aws_lambda_alias.ingress.name
  authorization_type = "NONE"
}

resource "aws_lambda_function_url" "cashfree" {
  function_name      = aws_lambda_function.cashfree_webhook.function_name
  qualifier          = aws_lambda_alias.cashfree_webhook.name
  authorization_type = "NONE"
}

###############################################################################
# Scheduled sweeps
###############################################################################

locals {
  sweeps = {
    reminder-sweep  = { schedule = "rate(5 minutes)", target = "reminder-worker" }
    outbox-relay    = { schedule = "rate(2 minutes)", target = "outbound-worker" }
    drift-eval      = { schedule = "cron(0 3 * * ? *)", target = "monitoring-worker" }
    regional-rollup = { schedule = "rate(1 hour)", target = "monitoring-worker" }
  }
}

resource "aws_cloudwatch_event_rule" "sweep" {
  for_each            = local.sweeps
  name                = "${local.name}-${each.key}"
  schedule_expression = each.value.schedule
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "sweep" {
  for_each = local.sweeps
  rule     = aws_cloudwatch_event_rule.sweep[each.key].name
  arn      = aws_lambda_alias.worker[each.value.target].arn
  input    = jsonencode({ job = each.key })
}

resource "aws_lambda_permission" "sweep" {
  for_each      = local.sweeps
  statement_id  = "AllowEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.worker[each.value.target].function_name
  qualifier     = aws_lambda_alias.worker[each.value.target].name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sweep[each.key].arn
}
