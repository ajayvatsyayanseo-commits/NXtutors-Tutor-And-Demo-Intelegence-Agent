###############################################################################
# demo-command-center — queues
#
# Five lanes, deliberately separate. A reminder storm or an analytics backlog
# must not be able to delay a payment webhook or a booking, and the only way to
# guarantee that is separate queues with separate concurrency:
#
#   1 payment            FIFO   highest priority, tiniest concurrency
#   2 scheduling         FIFO   per-conversation ordering
#   3 customer outbound  std    batched, the only lane that talks to Meta
#   4 reminders          std    time-driven, bursty, interruptible
#   5 analytics          std    forecasts, objections, rollups — never blocking
#
# FIFO only where ordering genuinely matters. A forecast for conversation A has
# no ordering relationship with one for conversation B, and making that queue
# FIFO would serialise it for nothing.
###############################################################################

locals {
  name = "demo-command-center-${var.environment}"

  tags = {
    Service     = "demo-command-center"
    Environment = var.environment
    ManagedBy   = "terraform"
    Owner       = "nxtutors-platform"
  }

  # Every queue's visibility timeout is 6x its consumer's timeout. Shorter and
  # SQS redelivers a message that is still being processed; the idempotency
  # table absorbs it, but the work was wasted and the queue looks backed up.
  visibility_multiplier = 6
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

###############################################################################
# Encryption — one Demo-owned key. Tutor's key is not reused.
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
# Lane 1 — payment. FIFO, and the smallest concurrency in the system.
###############################################################################

resource "aws_sqs_queue" "payment_dlq" {
  name                              = "${local.name}-payment-dlq.fifo"
  fifo_queue                        = true
  message_retention_seconds         = 1209600 # 14 days: money needs a long look
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300
  tags                              = local.tags
}

resource "aws_sqs_queue" "payment" {
  name       = "${local.name}-payment.fifo"
  fifo_queue = true

  # Dedup is supplied per message (the provider event id), never derived from
  # the body — Cashfree can legitimately send two events with identical bodies.
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  visibility_timeout_seconds        = var.payment_worker_timeout * local.visibility_multiplier
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.payment_dlq.arn
    # Deliberately low. A payment event that fails twice needs a person, not a
    # third attempt — the failure is almost always a reconciliation mismatch,
    # which retrying cannot fix.
    maxReceiveCount = 2
  })

  tags = local.tags
}

###############################################################################
# Lane 2 — scheduling. FIFO per conversation.
###############################################################################

resource "aws_sqs_queue" "scheduling_dlq" {
  name                              = "${local.name}-scheduling-dlq.fifo"
  fifo_queue                        = true
  message_retention_seconds         = 1209600
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300
  tags                              = local.tags
}

resource "aws_sqs_queue" "scheduling" {
  name       = "${local.name}-scheduling.fifo"
  fifo_queue = true

  content_based_deduplication = false
  # `messageGroup` scope with `perMessageGroupId` throughput: ordering is
  # preserved within a conversation while unrelated conversations run wide.
  # Without it, one slow conversation serialises every other one.
  deduplication_scope   = "messageGroup"
  fifo_throughput_limit = "perMessageGroupId"

  visibility_timeout_seconds        = var.orchestrator_timeout * local.visibility_multiplier
  message_retention_seconds         = 345600
  kms_master_key_id                 = aws_kms_key.main.id
  kms_data_key_reuse_period_seconds = 300

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.scheduling_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.tags
}

###############################################################################
# Lane 3 — customer outbound. Standard, batched, the only lane touching Meta.
###############################################################################

resource "aws_sqs_queue" "outbound_dlq" {
  name                      = "${local.name}-outbound-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.id
  tags                      = local.tags
}

resource "aws_sqs_queue" "outbound" {
  name                       = "${local.name}-outbound"
  visibility_timeout_seconds = var.outbound_worker_timeout * local.visibility_multiplier
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.main.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.outbound_dlq.arn
    # Higher than the decision lanes: delivery failures are usually transient
    # provider blips, and a lost reply is very visible to a parent.
    maxReceiveCount = 5
  })

  tags = local.tags
}

###############################################################################
# Lane 4 — reminders. Bursty and interruptible by design.
###############################################################################

resource "aws_sqs_queue" "reminders_dlq" {
  name                      = "${local.name}-reminders-dlq"
  message_retention_seconds = 604800
  kms_master_key_id         = aws_kms_key.main.id
  tags                      = local.tags
}

resource "aws_sqs_queue" "reminders" {
  name                       = "${local.name}-reminders"
  visibility_timeout_seconds = var.reminder_worker_timeout * local.visibility_multiplier
  message_retention_seconds  = 172800 # 2 days — a reminder older than that is moot
  kms_master_key_id          = aws_kms_key.main.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.reminders_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.tags
}

###############################################################################
# Lane 5 — analytics. Never blocks a customer.
###############################################################################

resource "aws_sqs_queue" "analytics_dlq" {
  name                      = "${local.name}-analytics-dlq"
  message_retention_seconds = 604800
  kms_master_key_id         = aws_kms_key.main.id
  tags                      = local.tags
}

resource "aws_sqs_queue" "analytics" {
  name                       = "${local.name}-analytics"
  visibility_timeout_seconds = var.analytics_worker_timeout * local.visibility_multiplier
  message_retention_seconds  = 345600
  kms_master_key_id          = aws_kms_key.main.id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.analytics_dlq.arn
    maxReceiveCount     = 3
  })

  tags = local.tags
}
