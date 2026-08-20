###############################################################################
# demo-command-center — IAM
#
# One role per function. The grants are the security model: a function that has
# no route to a capability also has no permission for it, so a compromised
# function cannot reach past its own job even if its code tried to.
#
# The three that matter most:
#
#   ingress          may enqueue. Cannot read the database, cannot call any
#                    provider, cannot activate anything. It is the internet-
#                    facing function, so its blast radius is capped hardest.
#   payment worker   may touch payment tables and Cashfree. Cannot send a
#                    WhatsApp message and cannot read arbitrary tutor data.
#   outbound worker  may call Meta. Holds NO database grant at all — it works
#                    from the message it was handed.
#
# No role uses Action "*" with Resource "*". Every rds-data grant is scoped to
# the one existing cluster; none of them can create one.
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

locals {
  roles = [
    "ingress",
    "orchestrator",
    "scheduling",
    "reminder",
    "outbound",
    "payment",
    "analytics",
    "monitoring",
    "ops-api",
  ]
}

resource "aws_iam_role" "function" {
  for_each           = toset(local.roles)
  name               = "${local.name}-${each.value}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "basic" {
  for_each   = aws_iam_role.function
  role       = each.value.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# NOTE: AWSLambdaVPCAccessExecutionRole is deliberately NOT attached to any
# role. No Demo function is VPC-attached — persistence is the Data API, which
# is a public AWS endpoint. That is what removes the need for a NAT Gateway.

###############################################################################
# Shared statement fragments
###############################################################################

data "aws_secretsmanager_secret" "app" {
  name = var.secret_name
}

locals {
  data_api_actions = [
    "rds-data:ExecuteStatement",
    "rds-data:BatchExecuteStatement",
    "rds-data:BeginTransaction",
    "rds-data:CommitTransaction",
    "rds-data:RollbackTransaction",
  ]

  # The EXISTING cluster. Demo creates no database; it is granted Data API
  # access to one that already exists and owns only the `demo_agent` schema
  # inside it.
  #
  # Empty in `postgres_dsn` mode, and then the grant is not merely pointed at
  # nothing — it is not issued at all. Every policy below iterates this list, so
  # a role in DSN mode carries no `rds-data` permission whatsoever. That is the
  # least-privilege position and it is also the only working one: an IAM
  # statement whose resource list is `[""]` is rejected at apply time.
  data_api_grant = var.persistence_mode == "data_api" ? [var.aurora_cluster_arn] : []
}

###############################################################################
# ingress — the only internet-facing function. Enqueue and nothing else.
###############################################################################

data "aws_iam_policy_document" "ingress" {
  statement {
    sid       = "EnqueueDecisionWork"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.scheduling.arn, aws_sqs_queue.payment.arn]
  }
  statement {
    sid       = "EncryptQueuePayloads"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    sid       = "ReadWebhookSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
  # No rds-data. No provider calls. No activation. Verified and enqueued only.
}

###############################################################################
# orchestrator — owns conversation state and fans out to capability lanes.
###############################################################################

data "aws_iam_policy_document" "orchestrator" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.scheduling.arn]
  }
  statement {
    sid     = "FanOutToCapabilityLanes"
    actions = ["sqs:SendMessage"]
    resources = [
      aws_sqs_queue.outbound.arn,
      aws_sqs_queue.reminders.arn,
      aws_sqs_queue.analytics.arn,
      aws_sqs_queue.payment.arn,
    ]
  }
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

###############################################################################
# scheduling — Google and the gateway. No payment, no Meta.
###############################################################################

data "aws_iam_policy_document" "scheduling" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.scheduling.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbound.arn, aws_sqs_queue.reminders.arn]
  }
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    sid       = "ScheduleRemindersAndExpiries"
    actions   = ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"]
    resources = ["arn:aws:scheduler:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:schedule/${aws_scheduler_schedule_group.reminders.name}/*"]
  }
  statement {
    sid       = "PassSchedulerRole"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.scheduler.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["scheduler.amazonaws.com"]
    }
  }
  # Deliberately absent: any Cashfree action, any subscription activation.
}

###############################################################################
# reminder — plans and enqueues. Cannot send; that is the outbound worker.
###############################################################################

data "aws_iam_policy_document" "reminder" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.reminders.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

###############################################################################
# outbound — the ONLY function that may call Meta.
#
# Holds no database grant. It works from the message it was handed, which is
# what makes "a compromised sender cannot read the conversation history" a
# property of IAM rather than of the code.
###############################################################################

data "aws_iam_policy_document" "outbound" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  # No rds-data. No scheduler. No payment.
}

###############################################################################
# payment — money. Narrow and deep.
###############################################################################

data "aws_iam_policy_document" "payment" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.payment.arn]
  }
  statement {
    sid       = "AnnounceActivationAndWelcome"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  # It can queue a message; it cannot send one. The outbound boundary still
  # applies every ownership, template and opt-out check.
}

###############################################################################
# analytics — forecasts, objections, discounts. Cheap and interruptible.
###############################################################################

data "aws_iam_policy_document" "analytics" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.analytics.arn]
  }
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.outbound.arn]
  }
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

###############################################################################
# monitoring — regional rollups. Reads widely, writes only its own tables.
###############################################################################

data "aws_iam_policy_document" "monitoring" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.analytics.arn]
  }
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    sid       = "PublishAlerts"
    actions   = ["sns:Publish"]
    resources = var.alarm_topic_arns
  }
}

###############################################################################
# ops-api — the sub-admin console. Scoped reads, explicit actions only.
###############################################################################

data "aws_iam_policy_document" "ops_api" {
  dynamic "statement" {
    for_each = local.data_api_grant
    content {
      sid       = "AuroraDataApiOnTheExistingClusterOnly"
      actions   = local.data_api_actions
      resources = [statement.value]
    }
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [data.aws_secretsmanager_secret.app.arn, var.aurora_secret_arn]
  }
  statement {
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
  # Region scoping is enforced in the application, not by IAM: the grant is per
  # cluster, and rows are filtered by the operator's authorised regions read
  # from the gateway. IAM cannot express "only this operator's regions".
}

###############################################################################
# Attach
###############################################################################

locals {
  policies = {
    ingress      = data.aws_iam_policy_document.ingress.json
    orchestrator = data.aws_iam_policy_document.orchestrator.json
    scheduling   = data.aws_iam_policy_document.scheduling.json
    reminder     = data.aws_iam_policy_document.reminder.json
    outbound     = data.aws_iam_policy_document.outbound.json
    payment      = data.aws_iam_policy_document.payment.json
    analytics    = data.aws_iam_policy_document.analytics.json
    monitoring   = data.aws_iam_policy_document.monitoring.json
    "ops-api"    = data.aws_iam_policy_document.ops_api.json
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.policies
  name     = "${local.name}-${each.key}"
  role     = aws_iam_role.function[each.key].id
  policy   = each.value
}

###############################################################################
# EventBridge Scheduler — reminders, hold expiry, tutor-request expiry
###############################################################################

resource "aws_scheduler_schedule_group" "reminders" {
  name = "${local.name}-reminders"
  tags = local.tags
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Without this, any account could assume the role via the scheduler
    # service principal. The confused-deputy guard is not optional.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = local.tags
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    sid     = "InvokeTheReminderWorkerOnly"
    actions = ["lambda:InvokeFunction"]
    # The alias, not the unqualified function: a schedule must fire whatever
    # `live` currently points at, and a rollback must take effect without
    # re-granting anything.
    resources = [
      aws_lambda_function.worker["reminder-worker"].arn,
      aws_lambda_alias.worker["reminder-worker"].arn,
    ]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name}-scheduler"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
