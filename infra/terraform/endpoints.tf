###############################################################################
# VPC endpoints — the thing that makes "no NAT Gateway" true
#
# main.tf has always *stated* that VPC-attached functions reach AWS services
# through endpoints. Nothing created them. A VPC-attached Lambda with no NAT
# and no endpoint cannot reach SQS, Secrets Manager, KMS or CloudWatch at all:
# every call hangs until the function's timeout, which presents as a slow,
# intermittent, extremely confusing outage.
#
# Two kinds, and the difference matters for cost:
#
#   Gateway endpoints (S3)          free, route-table entries
#   Interface endpoints (the rest)  hourly per AZ + per GB
#
# So S3 is a gateway endpoint, and the interface list is exactly the services
# the VPC-attached functions actually call — no more.
###############################################################################

locals {
  # Only the VPC-attached functions need these: `ingress` and `outbound-worker`
  # run outside the VPC (main.tf explains why).
  interface_endpoints = toset([
    "sqs",            # match worker -> outbound queue
    "secretsmanager", # runtime credentials
    "kms",            # decrypt SQS/S3 payloads
    "logs",           # structured logs and EMF metrics
    "monitoring",     # CloudWatch alarms from the scheduled job
    "xray",           # tracing_config { mode = "Active" }
  ])
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-vpce"
  description = "tutor-match-meta VPC interface endpoints"
  vpc_id      = var.vpc_id
  tags        = local.tags

  ingress {
    description     = "HTTPS from the service's Lambdas only"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.lambda.id]
  }
}

data "aws_route_tables" "private" {
  vpc_id = var.vpc_id
  filter {
    name   = "association.subnet-id"
    values = var.private_subnet_ids
  }
}

# S3 is a gateway endpoint: free, and needed for the analytics export and for
# Lambda's own artifact fetch.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = var.vpc_id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = data.aws_route_tables.private.ids
  tags              = merge(local.tags, { Name = "${local.name}-s3" })
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = var.vpc_id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = var.private_subnet_ids
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = merge(local.tags, { Name = "${local.name}-${each.value}" })
}
