###############################################################################
# demo-command-center — logs, metrics and alarms
#
# Log retention is finite everywhere. Alarms are grouped by what they mean for
# a person on call rather than by which AWS service emitted them:
#
#   money        — a payment mismatch or a failed activation. Pages.
#   correctness  — a state invariant or a double-book prevention. Pages.
#   customer     — a queue backing up, a DLQ filling, a reminder failing.
#   cost         — LLM spend and circuit state. Notifies, never pages.
###############################################################################

locals {
  all_functions = merge(
    { for k, v in aws_lambda_function.worker : k => v.function_name },
    {
      ingress          = aws_lambda_function.ingress.function_name
      cashfree-webhook = aws_lambda_function.cashfree_webhook.function_name
    }
  )

  namespace = "NXTutors/DemoCommandCenter"

  dlqs = {
    payment    = aws_sqs_queue.payment_dlq.name
    scheduling = aws_sqs_queue.scheduling_dlq.name
    outbound   = aws_sqs_queue.outbound_dlq.name
    reminders  = aws_sqs_queue.reminders_dlq.name
    analytics  = aws_sqs_queue.analytics_dlq.name
  }
}

resource "aws_cloudwatch_log_group" "function" {
  for_each          = local.all_functions
  name              = "/aws/lambda/${each.value}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.tags
}

###############################################################################
# Money — these page.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "payment_mismatch" {
  alarm_name          = "${local.name}-payment-mismatch"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "PaymentMismatch"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "A verified payment event did not reconcile against its order. A human must look."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "activation_failure" {
  alarm_name          = "${local.name}-activation-failure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ActivationFailure"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Payment taken, subscription not active."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "payment_dlq" {
  alarm_name          = "${local.name}-payment-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "A payment event reached the DLQ."
  dimensions          = { QueueName = aws_sqs_queue.payment_dlq.name }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

###############################################################################
# Correctness — a guard firing is an incident, not a warning.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "illegal_transition" {
  alarm_name          = "${local.name}-illegal-transition"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "IllegalTransitionRejected"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  # A handful is normal traffic (a parent replying to an old message). A spike
  # means a worker is firing triggers the machine does not accept.
  threshold          = 20
  alarm_description  = "State machine rejecting an unusual number of transitions."
  alarm_actions      = var.alarm_topic_arns
  treat_missing_data = "notBreaching"
  tags               = local.tags
}

resource "aws_cloudwatch_metric_alarm" "slot_hold_conflict" {
  alarm_name          = "${local.name}-slot-hold-conflict"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "SlotHoldConflict"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "Double-booking prevention firing often — availability data may be stale."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "signature_failure" {
  alarm_name          = "${local.name}-webhook-signature-failure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "WebhookSignatureFailure"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  # Not zero: a misconfigured secret produces a burst legitimately. A sustained
  # rate is someone probing the endpoint.
  threshold          = 5
  alarm_description  = "Invalid webhook signatures — misconfiguration or probing."
  alarm_actions      = var.alarm_topic_arns
  treat_missing_data = "notBreaching"
  tags               = local.tags
}

resource "aws_cloudwatch_metric_alarm" "guardrail_block" {
  alarm_name          = "${local.name}-outbound-guardrail-block"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "WebhookRejected"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "The output guardrail blocked a message. Something tried to send an unsafe claim."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

###############################################################################
# Customer experience
###############################################################################

resource "aws_cloudwatch_metric_alarm" "scheduling_queue_age" {
  alarm_name          = "${local.name}-scheduling-queue-age"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = var.queue_age_alarm_seconds
  alarm_description   = "Parents are waiting for a reply."
  dimensions          = { QueueName = aws_sqs_queue.scheduling.name }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  for_each = { for k, v in local.dlqs : k => v if k != "payment" }

  alarm_name          = "${local.name}-${each.key}-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Messages in the ${each.key} DLQ."
  dimensions          = { QueueName = each.value }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "function_errors" {
  for_each = local.all_functions

  alarm_name          = "${local.name}-${each.key}-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  dimensions          = { FunctionName = each.value }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "function_throttles" {
  for_each = local.all_functions

  alarm_name          = "${local.name}-${each.key}-throttles"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Reserved concurrency exhausted — raise it or shed load."
  dimensions          = { FunctionName = each.value }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "reminder_failures" {
  alarm_name          = "${local.name}-reminder-delivery-failure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ReminderDeliveryFailure"
  namespace           = local.namespace
  period              = 900
  statistic           = "Sum"
  threshold           = 10
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

###############################################################################
# Cost and health — notify, never page.
###############################################################################

resource "aws_cloudwatch_metric_alarm" "llm_cost" {
  alarm_name          = "${local.name}-llm-cost"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "LlmEstimatedCostMicros"
  namespace           = local.namespace
  period              = 3600
  statistic           = "Sum"
  threshold           = var.hourly_llm_cost_alarm_micros
  alarm_description   = "LLM spend above the hourly expectation."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "llm_budget_exhausted" {
  alarm_name          = "${local.name}-llm-budget-exhausted"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "LlmBudgetExhausted"
  namespace           = local.namespace
  period              = 900
  statistic           = "Sum"
  threshold           = 20
  alarm_description   = "Conversations hitting their LLM ceiling — a loop, or a budget set too low."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "circuit_opened" {
  alarm_name          = "${local.name}-circuit-opened"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "CircuitOpened"
  namespace           = local.namespace
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "A provider circuit opened. Check which, and its declared degradation."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "underperformance" {
  alarm_name          = "${local.name}-regional-underperformance"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "UnderperformanceAlert"
  namespace           = local.namespace
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "A region breached a monitoring rule (sample floor and cooldown already applied)."
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

variable "hourly_llm_cost_alarm_micros" {
  description = "Hourly LLM spend, in micro-units, above which to notify. A parameter, never a hardcoded price."
  type        = number
  default     = 2000000
}
