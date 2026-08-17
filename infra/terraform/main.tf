###############################################################################
# tutor-match-meta — serverless infrastructure
#
# Explicitly NOT provisioned, and asserted absent by
# tests/security/test_network_boundary.py:
#   * no NAT Gateway    — VPC functions reach AWS services through VPC endpoints
#   * no Fargate/ECS/EC2 — everything is Lambda
#   * no ElastiCache     — the shared store is PostgreSQL (cache/postgres_store.py)
#   * no second database — PostgreSQL is the only engine
#
# The consequence is designed for rather than worked around. Functions inside
# the VPC have no route to the public internet, and functions outside it have no
# route to PostgreSQL. **The database is never made publicly accessible to
# bridge that gap.** Instead the pipeline crosses the boundary on SQS FIFO:
#
#   API GW -> ingress        (internet)  validate, sign-check, enqueue
#          -> enrich.fifo
#          -> enrich-worker  (internet)  OpenAI + memory. No database grant.
#          -> match.fifo
#          -> match-worker   (VPC)       PostgreSQL/pgvector. No internet route.
#          -> outbound.fifo
#          -> outbound-worker(internet)  Meta Cloud API. No database grant.
#
# Every function declares its side as TMM_NETWORK_ZONE, and the test suite
# asserts that declaration matches whether the function has a `vpc_config`.
# See src/tutor_match_meta/contracts/enrichment.py for the full rationale.
###############################################################################

locals {
  name = "tutor-match-meta-${var.environment}"

  tags = {
    Service     = "tutor-match-meta"
    Environment = var.environment
    ManagedBy   = "terraform"
    Owner       = "nxtutors-platform"
  }

  # Bound so Lambda concurrency x pool size can never exhaust the database.
  # RDS Proxy multiplexes, but the ceiling still has to exist.
  worker_concurrency = var.match_worker_reserved_concurrency
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Encryption
###############################################################################

resource "aws_kms_key" "main" {
  description             = "${local.name} data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = local.tags
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}

###############################################################################
# Queues — FIFO for the decision path, standard for delivery
###############################################################################

resource "aws_sqs_queue" "enrich_dlq" {
  name                              = "${local.name}-enrich-dlq.fifo"
  fifo_queue                        = true
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300
  tags                              = local.tags
}

# The first hop. Ingress publishes here; the enrich worker — which lives
# OUTSIDE the VPC and is therefore the only function that can reach OpenAI —
# consumes it, attaches an EnrichmentV1, and forwards to the match queue.
#
# This queue exists because there is no NAT Gateway. Without it the VPC-attached
# match worker would have to call api.openai.com itself, over a route that does
# not exist, and would block until the client timeout on every single turn.
resource "aws_sqs_queue" "enrich" {
  name       = "${local.name}-enrich.fifo"
  fifo_queue = true

  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  visibility_timeout_seconds        = var.enrich_worker_timeout * 6
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.enrich_dlq.arn
    # Lower than the match queue on purpose: enrichment is best-effort, so a
    # record that fails three times is failing to *forward*, which is a real
    # fault and belongs in front of a human quickly.
    maxReceiveCount = 3
  })

  tags = local.tags
}

resource "aws_sqs_queue" "match_dlq" {
  name                              = "${local.name}-match-dlq.fifo"
  fifo_queue                        = true
  message_retention_seconds         = 1209600 # 14 days: enough to debug and redrive
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300
  tags                              = local.tags
}

resource "aws_sqs_queue" "match" {
  name       = "${local.name}-match.fifo"
  fifo_queue = true

  # Dedup is supplied per message (the provider message id), never derived from
  # the body — two genuinely different turns can have identical text.
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  # Must exceed the worker's timeout or SQS redelivers a message that is still
  # being processed, and the idempotency table absorbs work that was not wasted.
  visibility_timeout_seconds        = var.match_worker_timeout * 6
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.match_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.tags
}

resource "aws_sqs_queue" "outbound_dlq" {
  name                      = "${local.name}-outbound-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.id
  tags                      = local.tags
}

resource "aws_sqs_queue" "outbound" {
  name                       = "${local.name}-outbound"
  visibility_timeout_seconds = var.outbound_worker_timeout * 6
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.main.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.outbound_dlq.arn
    # Higher than the match queue: delivery failures are usually transient
    # provider blips, and a lost reply is very visible to a parent.
    maxReceiveCount = 5
  })

  tags = local.tags
}

###############################################################################
# Storage
###############################################################################

resource "aws_s3_bucket" "analytics" {
  bucket = "${local.name}-analytics"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "analytics" {
  bucket                  = aws_s3_bucket.analytics.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "analytics" {
  bucket = aws_s3_bucket.analytics.id
  rule {
    id     = "expire-exports"
    status = "Enabled"
    filter { prefix = "exports/" }
    expiration { days = 400 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
  # Staged feed pages are a work queue, not an archive: the ingest job
  # deletes each one as it applies it. This rule only catches pages that
  # failed to apply, so they cannot accumulate cost indefinitely while
  # still living long enough for someone to investigate.
  rule {
    id     = "expire-feed-staging"
    status = "Enabled"
    filter { prefix = "feed-staging/" }
    expiration { days = 7 }
    noncurrent_version_expiration { noncurrent_days = 3 }
  }
}

###############################################################################
# Secrets — referenced, never created with a value in state
###############################################################################

data "aws_secretsmanager_secret" "app" {
  name = var.secret_name
}

###############################################################################
# IAM — least privilege, one role per function
###############################################################################

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ingress" {
  name               = "${local.name}-ingress"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role" "enrich" {
  name               = "${local.name}-enrich"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role" "feed_fetcher" {
  name               = "${local.name}-feed-fetcher"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role" "outbound" {
  name               = "${local.name}-outbound"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role" "scheduled" {
  name               = "${local.name}-scheduled"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

# Ingress: enqueue only. It cannot read the database or any tutor data, which
# caps the blast radius of the one function exposed to the internet.
data "aws_iam_policy_document" "ingress" {
  statement {
    # The enrich queue only. Ingress cannot reach the match queue, so a
    # compromised internet-facing function cannot inject an envelope carrying
    # a forged EnrichmentV1 straight into the matching path.
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.enrich.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
}

# Feed fetcher: reads the website over the public internet and writes staged
# pages to one S3 prefix. No rds-db:connect and no queue access — it is
# outside the VPC and has no route to the database, and withholding the
# grant makes that structural rather than a matter of code choosing not to.
data "aws_iam_policy_document" "feed_fetcher" {
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.analytics.arn}/feed-staging/*"]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
}

# Enrich: reads the enrich queue, writes the match queue, reads secrets.
# Deliberately NO rds-db:connect — this function is outside the VPC and has
# no route to the database. Withholding the grant makes that structural
# rather than a matter of the code choosing not to connect.
data "aws_iam_policy_document" "enrich" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.enrich.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.match.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
}

data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.match.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
  statement {
    actions   = ["rds-db:connect"]
    resources = ["arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:${var.rds_proxy_resource_id}/${var.db_username}"]
  }
}

data "aws_iam_policy_document" "outbound" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
}

data "aws_iam_policy_document" "scheduled" {
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.analytics.arn}/exports/*"]
  }
  # Drains the staging prefix the internet-side fetcher writes. Delete is
  # required, not optional: an applied page must disappear or the next run
  # re-applies it forever.
  statement {
    actions   = ["s3:GetObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.analytics.arn}/feed-staging/*"]
  }
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.analytics.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["feed-staging/*"]
    }
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
  statement {
    actions   = ["rds-db:connect"]
    resources = ["arn:aws:rds-db:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:dbuser:${var.rds_proxy_resource_id}/${var.db_username}"]
  }
}

locals {
  role_policies = {
    ingress   = { role = aws_iam_role.ingress.id, json = data.aws_iam_policy_document.ingress.json }
    enrich    = { role = aws_iam_role.enrich.id, json = data.aws_iam_policy_document.enrich.json }
    feedfetch = { role = aws_iam_role.feed_fetcher.id, json = data.aws_iam_policy_document.feed_fetcher.json }
    worker    = { role = aws_iam_role.worker.id, json = data.aws_iam_policy_document.worker.json }
    outbound  = { role = aws_iam_role.outbound.id, json = data.aws_iam_policy_document.outbound.json }
    scheduled = { role = aws_iam_role.scheduled.id, json = data.aws_iam_policy_document.scheduled.json }
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.role_policies
  name     = "${local.name}-${each.key}"
  role     = each.value.role
  policy   = each.value.json
}

resource "aws_iam_role_policy_attachment" "basic" {
  for_each = {
    ingress   = aws_iam_role.ingress.name
    enrich    = aws_iam_role.enrich.name
    feedfetch = aws_iam_role.feed_fetcher.name
    worker    = aws_iam_role.worker.name
    outbound  = aws_iam_role.outbound.name
    scheduled = aws_iam_role.scheduled.name
  }
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy_attachment" "vpc" {
  for_each = {
    worker    = aws_iam_role.worker.name
    scheduled = aws_iam_role.scheduled.name
  }
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

###############################################################################
# Networking
###############################################################################

resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "tutor-match-meta lambdas"
  vpc_id      = var.vpc_id
  tags        = local.tags

  egress {
    description = "PostgreSQL via RDS Proxy"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.database_cidr_blocks
  }

  egress {
    description = "AWS service VPC endpoints"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.vpc_cidr_blocks
  }
}

###############################################################################
# Lambda functions
###############################################################################

locals {
  common_environment = {
    TMM_ENVIRONMENT        = var.environment
    TMM_AWS_REGION         = data.aws_region.current.name
    TMM_LOG_LEVEL          = var.log_level
    TMM_SECRETS_PREFIX     = "/${local.name}/"
    TMM_ENRICH_QUEUE_URL   = aws_sqs_queue.enrich.url
    TMM_MATCH_QUEUE_URL    = aws_sqs_queue.match.url
    TMM_OUTBOUND_QUEUE_URL = aws_sqs_queue.outbound.url
    TMM_ANALYTICS_BUCKET   = aws_s3_bucket.analytics.id
    TMM_POLICY_DIR         = "config/policies"
    TMM_DEFAULT_POLICY     = "regular_school_support.v1"
  }
}

resource "aws_lambda_function" "ingress" {
  function_name = "${local.name}-ingress"
  role          = aws_iam_role.ingress.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.ingress_handler"
  runtime       = "python3.12"
  architectures = ["arm64"] # cheaper per ms, and the deps are pure Python

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  # Short on purpose: ingress validates and enqueues. If it needs more than
  # this, something has leaked onto the hot path.
  timeout     = 10
  memory_size = 512

  # Outside the VPC: it talks only to SQS over the public API and never
  # touches the database. `network_zone=internet` makes that explicit to the
  # process, which then refuses to build a database session factory at all.
  environment {
    variables = merge(local.common_environment, { TMM_NETWORK_ZONE = "internet" })
  }

  tracing_config { mode = "Active" }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

resource "aws_lambda_function" "match_worker" {
  function_name = "${local.name}-match-worker"
  role          = aws_iam_role.worker.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.match_worker_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  timeout     = var.match_worker_timeout
  memory_size = 1024

  # Bounds concurrent database connections. Raising this without raising the
  # RDS Proxy connection limit is how the database gets exhausted.
  reserved_concurrent_executions = local.worker_concurrency

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  # `network_zone=vpc` is what stops this function building an OpenAI client.
  # It has no route to api.openai.com, so a provider here would block until
  # the client timeout on every turn and silently degrade to
  # deterministic-only extraction. The enrich worker did that work already.
  environment {
    variables = merge(local.common_environment, {
      TMM_CACHE_BACKEND = var.cache_backend
      TMM_NETWORK_ZONE  = "vpc"
    })
  }

  tracing_config { mode = "Active" }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

# The internet side of a turn. Outside the VPC by necessity: it calls OpenAI
# and the memory service, and with no NAT Gateway a VPC-attached function has
# no route to either. It holds no database grant and builds no session
# factory — see contracts/enrichment.py.
resource "aws_lambda_function" "enrich_worker" {
  function_name = "${local.name}-enrich-worker"
  role          = aws_iam_role.enrich.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.enrich_worker_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  timeout     = var.enrich_worker_timeout
  memory_size = 512

  # Bounds concurrent OpenAI calls, which is the real cost ceiling on this
  # path. It deliberately does NOT bound database connections: this function
  # opens none.
  reserved_concurrent_executions = var.enrich_worker_concurrency

  environment {
    variables = merge(local.common_environment, { TMM_NETWORK_ZONE = "internet" })
  }

  tracing_config { mode = "Active" }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

# Pages the website's signed tutor feed and stages each page in S3 for the
# VPC-side scheduled job to apply. Outside the VPC because the website is on
# the public internet; holds no database grant at all.
resource "aws_lambda_function" "feed_fetcher" {
  function_name = "${local.name}-feed-fetcher"
  role          = aws_iam_role.feed_fetcher.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.scheduled_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  # A full sweep of the tutor base, one page at a time, over the internet.
  timeout     = 300
  memory_size = 1024

  # One at a time. Two concurrent sweeps would stage duplicate pages, which
  # is harmless (the ingest upserts) but doubles the S3 and feed cost.
  reserved_concurrent_executions = 1

  environment {
    variables = merge(local.common_environment, { TMM_NETWORK_ZONE = "internet" })
  }

  tracing_config { mode = "Active" }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

resource "aws_lambda_function" "outbound_worker" {
  function_name = "${local.name}-outbound-worker"
  role          = aws_iam_role.outbound.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.outbound_worker_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  timeout     = var.outbound_worker_timeout
  memory_size = 512

  # Deliberately outside the VPC: it must reach graph.facebook.com, and with no
  # NAT Gateway a VPC-attached function has no route to the internet.
  environment {
    variables = merge(local.common_environment, {
      TMM_WHATSAPP_ENABLED = "true"
      TMM_NETWORK_ZONE     = "internet"
    })
  }

  tracing_config { mode = "Active" }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

resource "aws_lambda_function" "scheduled" {
  function_name = "${local.name}-scheduled"
  role          = aws_iam_role.scheduled.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.scheduled_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  timeout     = 300
  memory_size = 1024

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = merge(local.common_environment, { TMM_NETWORK_ZONE = "vpc" })
  }
  tracing_config { mode = "Active" }
  kms_key_arn = aws_kms_key.main.arn
  tags        = local.tags
}

###############################################################################
# Event sources
###############################################################################

resource "aws_lambda_event_source_mapping" "enrich" {
  event_source_arn = aws_sqs_queue.enrich.arn
  function_name    = aws_lambda_function.enrich_worker.arn

  # One at a time, same reason as the match queue: a batch would serialise
  # unrelated conversations behind each other.
  batch_size                         = 1
  function_response_types            = ["ReportBatchItemFailures"]
  maximum_batching_window_in_seconds = 0

  scaling_config { maximum_concurrency = var.enrich_worker_concurrency }
}

resource "aws_lambda_event_source_mapping" "match" {
  event_source_arn = aws_sqs_queue.match.arn
  function_name    = aws_lambda_function.match_worker.arn

  # One at a time: a batch of ten turns in one invocation would serialise
  # unrelated conversations behind each other.
  batch_size = 1

  # Without this, one poison record forces redelivery of its whole batch.
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config { maximum_concurrency = local.worker_concurrency }
}

resource "aws_lambda_event_source_mapping" "outbound" {
  event_source_arn                   = aws_sqs_queue.outbound.arn
  function_name                      = aws_lambda_function.outbound_worker.arn
  batch_size                         = 5
  maximum_batching_window_in_seconds = 2
  function_response_types            = ["ReportBatchItemFailures"]
}

###############################################################################
# Scheduled jobs — offline only, never on the WhatsApp path
###############################################################################

locals {
  schedules = {
    # `sync_projection` is deliberately absent: it fetches AND upserts, and
    # this function has no route to the website. The fetch half runs on
    # `feed_fetcher` (internet) and stages pages in S3; this half applies
    # them. See src/tutor_match_meta/sync/feed_staging.py.
    ingest_tutor_feed    = "rate(15 minutes)"
    check_staleness      = "rate(30 minutes)"
    relay_outbox         = "rate(5 minutes)"
    reconcile_projection = "cron(30 2 * * ? *)"
    retention_cleanup    = "cron(0 3 * * ? *)"
  }
}

# The internet half of the projection sync. Separate from `local.schedules`
# because it targets a different function on the other side of the network
# boundary; folding it into that map would silently point it at the VPC
# function, which cannot reach the website.
resource "aws_cloudwatch_event_rule" "feed_fetch" {
  name                = "${local.name}-stage-tutor-feed"
  schedule_expression = var.feed_fetch_schedule
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "feed_fetch" {
  rule  = aws_cloudwatch_event_rule.feed_fetch.name
  arn   = aws_lambda_function.feed_fetcher.arn
  input = jsonencode({ job = "stage_tutor_feed" })
}

resource "aws_lambda_permission" "feed_fetch" {
  statement_id  = "AllowEventBridge-stage-tutor-feed"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.feed_fetcher.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.feed_fetch.arn
}

resource "aws_cloudwatch_event_rule" "job" {
  for_each            = local.schedules
  name                = "${local.name}-${replace(each.key, "_", "-")}"
  schedule_expression = each.value
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "job" {
  for_each = local.schedules
  rule     = aws_cloudwatch_event_rule.job[each.key].name
  arn      = aws_lambda_function.scheduled.arn
  input    = jsonencode({ job = each.key })
}

resource "aws_lambda_permission" "job" {
  for_each      = local.schedules
  statement_id  = "AllowEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.scheduled.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.job[each.key].arn
}

###############################################################################
# Logs and alarms
###############################################################################

locals {
  functions = {
    ingress         = aws_lambda_function.ingress.function_name
    enrich-worker   = aws_lambda_function.enrich_worker.function_name
    feed-fetcher    = aws_lambda_function.feed_fetcher.function_name
    match-worker    = aws_lambda_function.match_worker.function_name
    outbound-worker = aws_lambda_function.outbound_worker.function_name
    scheduled       = aws_lambda_function.scheduled.function_name
  }
}

resource "aws_cloudwatch_log_group" "function" {
  for_each          = local.functions
  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.tags
}

# Anything in a DLQ means a parent's message was never answered.
resource "aws_cloudwatch_metric_alarm" "match_dlq" {
  alarm_name          = "${local.name}-match-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Messages in the match DLQ: parents did not get a reply."
  dimensions          = { QueueName = aws_sqs_queue.match_dlq.name }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "outbound_dlq" {
  alarm_name          = "${local.name}-outbound-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Undelivered WhatsApp replies."
  dimensions          = { QueueName = aws_sqs_queue.outbound_dlq.name }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "queue_age" {
  alarm_name          = "${local.name}-match-queue-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 120
  alarm_description   = "Parents are waiting more than two minutes for a reply."
  dimensions          = { QueueName = aws_sqs_queue.match.name }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "${local.name}-worker-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  dimensions          = { FunctionName = aws_lambda_function.match_worker.function_name }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

# The anti-fabrication guard tripping is a correctness incident, not a warning.
resource "aws_cloudwatch_metric_alarm" "fabrication" {
  alarm_name          = "${local.name}-fabrication-violations"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FabricationViolations"
  namespace           = "NXTutors/TutorMatchMeta"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "The evidence guard blocked an unsupported claim. Investigate."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "projection_stale" {
  alarm_name          = "${local.name}-projection-stale"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ProjectionStalenessHours"
  namespace           = "NXTutors/TutorMatchMeta"
  period              = 1800
  statistic           = "Maximum"
  threshold           = 24
  alarm_description   = "Tutor projection older than the recommendable window."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "breaching" # no data means the sync job is not running
  tags                = local.tags
}
