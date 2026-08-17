###############################################################################
# API Gateway — the two HTTP surfaces
#
# Neither existed before this file: `aws_lambda_function.ingress` was deployed
# with no trigger at all, and the FastAPI handoff endpoint that Lead Intake is
# supposed to call had no Lambda and no route. The service could not have
# received a single message.
#
# Two separate HTTP APIs rather than one with two routes, because they have
# genuinely different threat models and different throttles:
#
#   ingress    signed with HMAC (security/signing.py), enqueue-only, no
#              database access, tightest throttle
#   internal   shared-secret header, calls the full matching path inline under
#              Lead Intake's 2-second budget
###############################################################################

resource "aws_apigatewayv2_api" "ingress" {
  name          = "${local.name}-ingress"
  protocol_type = "HTTP"
  description   = "Signed event ingress. Validates, rate-limits and enqueues."

  # No CORS: there is no browser client, and a permissive default is how an
  # internal API acquires one by accident.
  tags = local.tags
}

resource "aws_apigatewayv2_stage" "ingress" {
  api_id      = aws_apigatewayv2_api.ingress.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    # The outermost rate limit. The application's layered limiter is the one
    # that attributes abuse to a conversation; this one just stops a flood
    # reaching Lambda at all.
    throttling_burst_limit   = var.api_throttle_burst
    throttling_rate_limit    = var.api_throttle_rate
    detailed_metrics_enabled = true
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    # Deliberately no request body and no query string: the body is a parent's
    # message. `errorResponseType` is what an on-call engineer actually needs.
    format = jsonencode({
      requestId          = "$context.requestId"
      traceId            = "$context.requestHeaderOverride"
      httpMethod         = "$context.httpMethod"
      routeKey           = "$context.routeKey"
      status             = "$context.status"
      responseLatency    = "$context.responseLatency"
      integrationLatency = "$context.integrationLatency"
      errorType          = "$context.error.responseType"
      sourceIp           = "$context.identity.sourceIp"
    })
  }

  tags = local.tags
}

resource "aws_cloudwatch_log_group" "api_access" {
  name              = "/aws/apigateway/${local.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
  tags              = local.tags
}

resource "aws_apigatewayv2_integration" "ingress" {
  api_id                 = aws_apigatewayv2_api.ingress.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ingress.invoke_arn
  payload_format_version = "2.0"
  # Below the function's 10s timeout, so API Gateway is never the thing that
  # gives up first and leaves the Lambda still running.
  timeout_milliseconds = 8000
}

resource "aws_apigatewayv2_route" "ingress" {
  api_id    = aws_apigatewayv2_api.ingress.id
  route_key = "POST /ingress"
  target    = "integrations/${aws_apigatewayv2_integration.ingress.id}"
}

resource "aws_lambda_permission" "ingress_api" {
  statement_id  = "AllowIngressApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingress.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ingress.execution_arn}/*/*"
}

###############################################################################
# Internal API — the Lead Intake handoff endpoint
###############################################################################

resource "aws_lambda_function" "internal_api" {
  function_name = "${local.name}-internal-api"
  role          = aws_iam_role.worker.arn
  handler       = "tutor_match_meta.handlers.lambda_entry.api_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]

  s3_bucket = var.artifact_bucket
  s3_key    = var.artifact_key

  # Lead Intake times out at 2s (contracts/handoff.py). A longer timeout here
  # would only mean we are still working after the caller has given up.
  timeout     = 10
  memory_size = 1024

  # Same reserved concurrency budget as the worker: both open database
  # connections through the proxy, and the ceiling has to cover the sum.
  reserved_concurrent_executions = var.internal_api_reserved_concurrency

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  # VPC-attached, so `network_zone=vpc`: the handoff path reads conversation
  # state and the tutor projection, and must not attempt an LLM call — this
  # function has no route to one, and Lead Intake gives up after 2s anyway.
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

resource "aws_apigatewayv2_api" "internal" {
  name          = "${local.name}-internal"
  protocol_type = "HTTP"
  description   = "Lead Intake handoff, health, readiness and version."
  tags          = local.tags
}

resource "aws_apigatewayv2_stage" "internal" {
  api_id      = aws_apigatewayv2_api.internal.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit   = var.api_throttle_burst
    throttling_rate_limit    = var.api_throttle_rate
    detailed_metrics_enabled = true
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_access.arn
    format = jsonencode({
      requestId = "$context.requestId"
      routeKey  = "$context.routeKey"
      status    = "$context.status"
      latency   = "$context.responseLatency"
      errorType = "$context.error.responseType"
    })
  }

  tags = local.tags
}

resource "aws_apigatewayv2_integration" "internal" {
  api_id                 = aws_apigatewayv2_api.internal.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.internal_api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 9000
}

# One catch-all route: FastAPI owns the routing, and duplicating the path list
# here is a second source of truth that silently drifts.
resource "aws_apigatewayv2_route" "internal" {
  api_id    = aws_apigatewayv2_api.internal.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.internal.id}"
}

resource "aws_lambda_permission" "internal_api" {
  statement_id  = "AllowInternalApi"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.internal_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.internal.execution_arn}/*/*"
}

###############################################################################
# Alarms on the HTTP surfaces
###############################################################################

resource "aws_cloudwatch_metric_alarm" "ingress_5xx" {
  alarm_name          = "${local.name}-ingress-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "5xx"
  namespace           = "AWS/ApiGateway"
  period              = 300
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = "Ingress is failing. Messages are not reaching the queue."
  dimensions          = { ApiId = aws_apigatewayv2_api.ingress.id }
  alarm_actions       = var.alarm_topic_arns
  treat_missing_data  = "notBreaching"
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "internal_latency" {
  alarm_name          = "${local.name}-internal-p95-latency"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "Latency"
  namespace           = "AWS/ApiGateway"
  period              = 300
  extended_statistic  = "p95"
  # Lead Intake gives us 2 seconds. Alarming at 1.8s leaves room to react
  # before the caller starts timing out and retrying, which turns one message
  # into three.
  threshold          = 1800
  alarm_description  = "Handoff p95 is approaching Lead Intake's 2s timeout."
  dimensions         = { ApiId = aws_apigatewayv2_api.internal.id }
  alarm_actions      = var.alarm_topic_arns
  treat_missing_data = "notBreaching"
  tags               = local.tags
}
