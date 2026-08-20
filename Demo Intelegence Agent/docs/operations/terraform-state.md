# Terraform state — an operator decision, escalated rather than made silently

## The situation

The Tutor Intelligence stack uses `backend "s3"`. Demo is prohibited from
creating a new S3 bucket, and reusing Tutor's state bucket would put two
independent lifecycles in one blast radius — a mistaken `terraform destroy` in
one workspace would be operating in the other's bucket.

So `Demo Intelegence Agent/infra/terraform/versions.tf` has **no backend block**,
and this is the decision record rather than a silent default.

## The three options

### A. An already-approved non-S3 backend — preferred

If the organisation already runs Terraform Cloud, an HTTP backend, or a
Postgres backend, use it. `backend.hcl.example` shows both shapes.

```bash
terraform init -backend-config=backend.hcl
```

**Why preferred:** state locking, history and encryption without creating any
new resource.

### B. Local state with a documented handover — acceptable for one operator

```bash
terraform init
```

**Acceptable when** exactly one person deploys and the state file is backed up
somewhere durable outside the repository. `.gitignore` already excludes
`*.tfstate`.

**Not acceptable when** more than one person deploys. There is no locking, and
two concurrent applies corrupt state silently.

### C. A new S3 state bucket — requires an explicit owner override

This is the option the constraint forbids. It is listed because it is the
obvious answer and someone will propose it: if the owner decides the constraint
should not apply to state storage, that decision should be recorded here with a
date and a name, and `scan_prohibited.py` updated to permit it.

Until then, `scan_prohibited.py` **fails the build** on a `backend "s3"` block.

## What state does and does not contain

Terraform state for this stack contains ARNs, queue URLs, function names and
IAM policy documents. It does **not** contain:

* provider credentials — those are Secrets Manager references, never values;
* the Aurora credential — referenced by ARN;
* customer data of any kind.

That is why option B is tolerable at all. It would not be if secrets were
materialised into state.

## Current status

**Undecided.** No backend is configured, so `terraform init` produces local
state. Before the first production deploy, the operator must choose A or B and
record it below.

| Date | Decision | Decided by |
|---|---|---|
| — | pending | — |
