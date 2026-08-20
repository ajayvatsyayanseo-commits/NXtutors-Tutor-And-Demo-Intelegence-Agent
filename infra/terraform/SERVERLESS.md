# Fully serverless — what changed and what it costs

**Date:** 2026-08-18

The Tutor stack used to attach three functions to a VPC so they could reach
PostgreSQL through an RDS Proxy. That required six interface VPC endpoints,
two security groups and the proxy itself — all billed by the hour whether or
not a single message was processed.

They are gone. Neither stack now creates a VPC, a NAT Gateway, a VPC endpoint,
a security group, a container or an RDS Proxy. **Nothing in either stack is
billed while idle** except CloudWatch log storage.

## Removed

| Resource | Count |
| --- | ---: |
| `aws_vpc_endpoint` (1 gateway + 6 interface) | 7 |
| `aws_security_group` | 2 |
| `vpc_config` blocks (match-worker, scheduled, internal-api) | 3 |
| `AWSLambdaVPCAccessExecutionRole` attachments | 2 |
| `rds-db:connect` IAM statements | 2 |
| Variables: `vpc_id`, `private_subnet_ids`, `vpc_cidr_blocks`, `database_cidr_blocks`, `rds_proxy_resource_id`, `rds_proxy_name` | 6 |

Added: `db_instance_identifier`, because the connection alarm now watches the
instance rather than the proxy.

## The trade this makes — read before deploying

**The database must accept connections from the public internet.** A Lambda
outside a VPC has no stable source address, so the RDS security group cannot
be narrowed to one. It has to allow `0.0.0.0/0` on 5432.

What still protects it:

* TLS is required on every connection (`?ssl=require` in the DSN);
* the credential lives in Secrets Manager and is never an environment variable;
* the database user owns only its own schema.

What no longer protects it: network reachability. Anyone who obtains the
credential can connect from anywhere. **Rotate it on any suspicion, and do not
reuse it anywhere else.**

**There is no connection pool in front of the database.** RDS Proxy was doing
that job. Reserved concurrency is now the connection ceiling directly:

    match_worker_reserved_concurrency + internal_api_reserved_concurrency
        must stay below the instance's max_connections

Raising either without checking `max_connections` exhausts the database — and
it is shared with `demo_command_center`, so that is an outage for a service
with nothing to do with tutoring. The `*-db-connections` alarm watches exactly
this.

## `TMM_NETWORK_ZONE` still means something

It was never really about the network. It declares what a function may talk
to: `vpc` means "database-side — opens a session, makes no LLM call";
`internet` means "no database grant at all".

The match worker could now technically reach `api.openai.com` and still must
not: the enrich worker already did that extraction, and a second one would pay
twice for a worse answer. `tests/security/test_network_boundary.py` asserts
both halves — that every function declares a zone, and that **no function is
VPC-attached**.

## The alternative, if the open security group is unacceptable

Move the database to **Aurora Serverless v2 with the RDS Data API**. The Data
API is a public AWS endpoint authenticated with IAM, so there is no port to
open and no credential to leak, it pools connections itself, and the database
scales toward zero as well. Demo's Data API backend is already written and
tested; this would also settle its `persistence_mode` choice.

That is a real migration — new cluster, dump and restore of both schemas, a
cutover — not a configuration change.
