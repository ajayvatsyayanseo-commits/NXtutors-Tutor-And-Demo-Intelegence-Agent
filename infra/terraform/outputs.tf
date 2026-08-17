output "match_queue_url" {
  value       = aws_sqs_queue.match.url
  description = "FIFO queue consumed by the match worker."
}

output "outbound_queue_url" {
  value       = aws_sqs_queue.outbound.url
  description = "Queue consumed by the outbound worker."
}

output "match_dlq_url" {
  value       = aws_sqs_queue.match_dlq.url
  description = "Redrive source for failed turns."
}

output "ingress_function_name" {
  value = aws_lambda_function.ingress.function_name
}

output "match_worker_function_name" {
  value = aws_lambda_function.match_worker.function_name
}

output "analytics_bucket" {
  value = aws_s3_bucket.analytics.id
}

output "kms_key_arn" {
  value = aws_kms_key.main.arn
}

output "ingress_url" {
  value       = "${aws_apigatewayv2_api.ingress.api_endpoint}/ingress"
  description = "Signed event ingress. Callers POST here."
}

output "internal_api_url" {
  value       = aws_apigatewayv2_api.internal.api_endpoint
  description = "Lead Intake sets TUTOR_MATCHING_AGENT_WEBHOOK_URL to <this>/internal/v1/handoff."
}

output "health_url" {
  value = "${aws_apigatewayv2_api.internal.api_endpoint}/internal/v1/health"
}

output "version_url" {
  value       = "${aws_apigatewayv2_api.internal.api_endpoint}/internal/v1/version"
  description = "Deployed app/commit/schema/policy/prompt versions. Requires the internal secret."
}

output "glue_database" {
  value       = var.glue_enabled ? aws_glue_catalog_database.analytics[0].name : ""
  description = "Catalogue over the sanitised analytics export."
}
