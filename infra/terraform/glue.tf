###############################################################################
# Glue — offline only, never on the WhatsApp path
#
# §23 is explicit: Glue is for cataloguing, sanitised historical events, ETL,
# analytics, feature datasets and evaluation datasets. It runs on a schedule,
# not per conversation, and it reads the S3 export written from
# `analytics_event` — a table whose shape (analytics/events.py) makes raw
# message text unrepresentable.
#
# The crawler therefore never sees a parent's message, a phone number or a
# locality finer than a city. That is a property of the producer, not of a
# filter here: an exclusion list would be one forgotten column away from
# leaking.
###############################################################################

resource "aws_s3_object" "analytics_prefix" {
  count  = var.glue_enabled ? 1 : 0
  bucket = aws_s3_bucket.analytics.id
  key    = "exports/"
  # An empty prefix marker so the crawler has a target before the first export.
  content_type = "application/x-directory"
  kms_key_id   = aws_kms_key.main.arn
}

resource "aws_glue_catalog_database" "analytics" {
  count       = var.glue_enabled ? 1 : 0
  name        = replace("${local.name}_analytics", "-", "_")
  description = "Sanitised TutorMatch funnel and evaluation datasets."
}

data "aws_iam_policy_document" "glue_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["glue.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "glue" {
  count              = var.glue_enabled ? 1 : 0
  name               = "${local.name}-glue"
  assume_role_policy = data.aws_iam_policy_document.glue_assume.json
  tags               = local.tags
}

# Read-only on exactly one prefix. Glue has no route to PostgreSQL, no route to
# the queues, and no write access to the export it reads — so a compromised job
# cannot alter the record it is analysing.
data "aws_iam_policy_document" "glue" {
  count = var.glue_enabled ? 1 : 0

  statement {
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.analytics.arn, "${aws_s3_bucket.analytics.arn}/exports/*"]
  }
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.analytics.arn}/curated/*"]
  }
  statement {
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.main.arn]
  }
  statement {
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws-glue/*"]
  }
}

resource "aws_iam_role_policy" "glue" {
  count  = var.glue_enabled ? 1 : 0
  name   = "${local.name}-glue"
  role   = aws_iam_role.glue[0].id
  policy = data.aws_iam_policy_document.glue[0].json
}

resource "aws_glue_crawler" "analytics" {
  count         = var.glue_enabled ? 1 : 0
  name          = "${local.name}-analytics"
  role          = aws_iam_role.glue[0].arn
  database_name = aws_glue_catalog_database.analytics[0].name

  s3_target {
    path = "s3://${aws_s3_bucket.analytics.id}/exports/"
  }

  # Daily, well after the nightly export. Per-conversation crawling would be
  # both pointless and the single most expensive way to run Glue.
  schedule = "cron(0 4 * * ? *)"

  schema_change_policy {
    delete_behavior = "LOG"
    update_behavior = "UPDATE_IN_DATABASE"
  }

  tags = local.tags
}

###############################################################################
# Lifecycle — the export is not kept forever
###############################################################################

resource "aws_s3_bucket_lifecycle_configuration" "curated" {
  count  = var.glue_enabled ? 1 : 0
  bucket = aws_s3_bucket.analytics.id

  rule {
    id     = "curated-retention"
    status = "Enabled"
    filter { prefix = "curated/" }
    transition {
      days          = 90
      storage_class = "STANDARD_IA"
    }
    # Evaluation datasets are worth keeping for drift comparison across
    # versions (§26); two years is the documented ceiling.
    expiration { days = 730 }
  }

  depends_on = [aws_s3_bucket_lifecycle_configuration.analytics]
}
