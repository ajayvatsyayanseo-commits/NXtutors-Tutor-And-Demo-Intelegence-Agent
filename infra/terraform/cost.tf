###############################################################################
# Cost controls and the alarms that are not about correctness
#
# Two different clocks, which is why there are two mechanisms:
#
#   AWS Budgets      slow (hours), catches a structural change — a new
#                    always-on resource, a runaway crawler
#   CloudWatch alarm fast (minutes), catches a runaway *rate* — a redelivery
#                    loop burning model calls
#
# Model spend needs the fast one. A budget alert that arrives the next morning
# is a post-mortem, not a control.
###############################################################################

resource "aws_budgets_budget" "service" {
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Service$tutor-match-meta"]
  }

  # 80% actual: something changed, look now.
  dynamic "notification" {
    for_each = length(var.budget_notification_emails) > 0 ? [1] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 80
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = var.budget_notification_emails
    }
  }

  # 100% forecast: the month is on track to overrun, act before it does.
  dynamic "notification" {
    for_each = length(var.budget_notification_emails) > 0 ? [1] : []
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = 100
      threshold_type             = "PERCENTAGE"
      notification_type          = "FORECASTED"
      subscriber_email_addresses = var.budget_notification_emails
    }
  }
}

###############################################################################
# Fast cost alarms — evaluated on the service's own EMF metrics
###############################################################################

resource "aws_cloudwatch_metric_alarm" "llm_spend" {
  alarm_name          = "${local.name}-llm-spend-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "LlmCostMicros"
  namespace           = "NXTutors/TutorMatchMeta"
  period              = 3600
  statistic           = "Sum"
  # Hourly ceiling derived from the monthly budget, with headroom for a busy
  # hour: monthly / 730 hours, x3.
  threshold = ceil(var.llm_monthly_budget_micros / 730 * 3)
  alarm_description = join(" ", [
    "Model spend is running above the monthly budget's hourly rate.",
    "Runbook: docs/runbooks/high-llm-spend.md.",
    "The immediate control is the LLM_PAUSED kill switch, which degrades to",
    "deterministic matching rather than stopping the service."
  ])
  alarm_actions      = var.alarm_topic_arns
  treat_missing_data = "notBreaching"
  tags               = local.tags
}

resource "aws_cloudwatch_metric_alarm" "llm_budget_exhausted" {
  alarm_name          = "${local.name}-llm-budget-exhausted"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "LlmBudgetExceeded"
  namespace           = "NXTutors/TutorMatchMeta"
  period              = 900
  statistic           = "Sum"
  threshold           = 10
  alarm_description = join(" ", [
    "Conversations are hitting their per-conversation model budget.",
    "Usually a redelivery loop rather than genuine demand — check",
    "DuplicateEvents before raising the ceiling."
  ])
  alarm_actions      = var.alarm_topic_arns
  treat_missing_data = "notBreaching"
  tags               = local.tags
}

###############################################################################
# Quality and safety alarms — the rollback triggers of §33
###############################################################################

locals {
  # Each entry is a rollback trigger. The `alarm_description` names the runbook
  # so an on-call engineer is never guessing at 3am.
  quality_alarms = {
    output-guard-rejections = {
      metric      = "OutputGuardRejected"
      period      = 300
      threshold   = 0
      statistic   = "Sum"
      periods     = 1
      description = "A generated message failed validation and was replaced. Rollback trigger: prompt version. Runbook: docs/runbooks/bad-prompt.md"
    }
    no-match-rate = {
      metric      = "NoMatch"
      period      = 900
      threshold   = 20
      statistic   = "Sum"
      periods     = 2
      description = "No-match rate abnormal. Check projection freshness first, then the scoring policy. Runbook: docs/runbooks/high-no-match.md"
    }
    human-handoff-spike = {
      metric      = "HumanHandoff"
      period      = 900
      threshold   = 15
      statistic   = "Sum"
      periods     = 2
      description = "HITL escalations spiking. Rollback trigger: scoring-policy version. Runbook: docs/runbooks/bad-scoring-policy.md"
    }
    injection-campaign = {
      metric      = "InjectionDetected"
      period      = 300
      threshold   = 25
      statistic   = "Sum"
      periods     = 1
      description = "Sustained prompt-injection attempts. Runbook: docs/runbooks/security-alarm.md"
    }
    duplicate-spike = {
      metric      = "DuplicateEvents"
      period      = 300
      threshold   = 50
      statistic   = "Sum"
      periods     = 2
      description = "Duplicate delivery spike — a caller is retrying. Verify no second side effect occurred. Runbook: docs/runbooks/duplicate-sends.md"
    }
    optimistic-lock-conflicts = {
      metric      = "OptimisticLockConflict"
      period      = 300
      threshold   = 20
      statistic   = "Sum"
      periods     = 2
      description = "Conversation state contention. Usually FIFO grouping is not being honoured upstream. Runbook: docs/runbooks/queue-backlog.md"
    }
    outbox-dead = {
      metric      = "OutboxDead"
      period      = 900
      threshold   = 0
      statistic   = "Sum"
      periods     = 1
      description = "An outbound message can never be delivered. A parent is waiting. Runbook: docs/runbooks/dlq-replay.md"
    }
    kill-switch-active = {
      metric      = "KillSwitchActive"
      period      = 900
      threshold   = 0
      statistic   = "Sum"
      periods     = 4
      description = "A kill switch has been held down for an hour. Confirm it is intentional. Runbook: docs/runbooks/emergency-pause.md"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "quality" {
  for_each = local.quality_alarms

  alarm_name          = "${local.name}-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = each.value.periods
  metric_name         = each.value.metric
  namespace           = "NXTutors/TutorMatchMeta"
  period              = each.value.period
  statistic           = each.value.statistic
  threshold           = each.value.threshold
  alarm_description   = each.value.description
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

###############################################################################
# Database saturation — the one alarm that is about someone else's blast radius
###############################################################################

resource "aws_cloudwatch_metric_alarm" "db_connections" {
  alarm_name          = "${local.name}-proxy-connections"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DatabaseConnections"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Maximum"
  # The ceiling this service may consume. It shares the instance with
  # demo_command_center, so exhausting connections is an outage for a service
  # that has nothing to do with tutoring.
  threshold = var.match_worker_reserved_concurrency + var.internal_api_reserved_concurrency
  alarm_description = join(" ", [
    "TutorMatch is using more proxy connections than its concurrency ceiling",
    "should allow. Raising reserved concurrency without raising the proxy",
    "limit is how the shared database gets exhausted.",
    "Runbook: docs/runbooks/db-outage.md"
  ])
  dimensions         = { DBProxyName = var.rds_proxy_name }
  alarm_actions      = var.alarm_topic_arns
  treat_missing_data = "notBreaching"
  tags               = local.tags
}
