"""Append the complete `DCC_*` block to the shared repository `.env`.

Both agents read one `.env`. This appends every Demo setting — including the
ones with no value yet, so an operator can see the whole surface and fill in
blanks rather than discovering a missing key at cold start.

**Never overwrites an existing key.** A `DCC_*` line already in the file is
left exactly as it is, so re-running this is safe and a hand-edited value is
never clobbered. Existing `TMM_*` lines are never touched at all.

    python scripts/merge_env.py            # show what would be added
    python scripts/merge_env.py --write    # append the missing keys
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
ENV = REPO_ROOT / ".env"

#: `(key, value, comment)`. An empty value means "must be supplied before this
#: capability works"; `dcc-doctor` reports each one as a gap.
BLOCKS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "Demo Command Center — runtime",
        [
            ("DCC_ENVIRONMENT", "local", "local | dev | staging | production"),
            ("DCC_SERVICE_NAME", "demo-command-center-agent", ""),
            ("DCC_LOG_LEVEL", "INFO", "DEBUG | INFO | WARNING | ERROR"),
            ("DCC_METRICS_ENABLED", "true", ""),
            ("DCC_DEFAULT_TIMEZONE", "Asia/Kolkata", "IANA name; every stored instant is UTC"),
        ],
    ),
    (
        "Demo Command Center — persistence (shares the Tutor database)",
        [
            (
                "DCC_PERSISTENCE_MODE",
                "postgres_dsn",
                "postgres_dsn | data_api | lambda_proxy | memory",
            ),
            (
                "DCC_POSTGRES_DSN",
                "",
                "blank = inherit TMM_POSTGRES_DSN above. One database, one URL.",
            ),
            ("DCC_AURORA_SCHEMA", "demo_agent", "Demo owns this schema; Tutor owns tutor_match"),
            ("DCC_POSTGRES_POOL_MIN", "1", ""),
            ("DCC_POSTGRES_POOL_MAX", "5", ""),
            ("DCC_POSTGRES_REQUIRE_TLS", "true", ""),
            ("DCC_DB_STATEMENT_TIMEOUT_MS", "5000", ""),
            ("DCC_DB_MAX_RETRIES", "2", ""),
            ("DCC_AURORA_CLUSTER_ARN", "", "only for persistence_mode=data_api"),
            ("DCC_AURORA_SECRET_ARN", "", "only for persistence_mode=data_api"),
            ("DCC_AURORA_DATABASE", "demo_command_center", ""),
            ("DCC_PERSISTENCE_LAMBDA_ARN", "", "only for persistence_mode=lambda_proxy"),
        ],
    ),
    (
        "Demo Command Center — AWS",
        [
            ("DCC_AWS_REGION", "ap-south-1", ""),
            ("DCC_WORK_QUEUE_URL", "", "scheduling FIFO queue"),
            ("DCC_OUTBOUND_QUEUE_URL", "", "the only queue the sender reads"),
            ("DCC_PAYMENT_QUEUE_URL", "", ""),
            ("DCC_REMINDER_QUEUE_URL", "", ""),
            ("DCC_ANALYTICS_QUEUE_URL", "", ""),
            ("DCC_SCHEDULER_GROUP_NAME", "demo-command-center", ""),
            ("DCC_SCHEDULER_ROLE_ARN", "", "required before reminders fire on time"),
            ("DCC_EVENT_BUS_NAME", "default", ""),
            ("DCC_KMS_KEY_ARN", "", ""),
            ("DCC_SECRETS_PREFIX", "/demo-command-center/", ""),
        ],
    ),
    (
        "Demo Command Center — Meta WhatsApp (Demo owns the demo conversation)",
        [
            ("DCC_META_ENABLED", "false", "true requires the four secrets below"),
            ("DCC_META_APP_ID", "", ""),
            ("DCC_META_APP_SECRET", "", "signs X-Hub-Signature-256"),
            ("DCC_META_VERIFY_TOKEN", "", "GET webhook handshake"),
            ("DCC_META_ACCESS_TOKEN", "", ""),
            ("DCC_META_PHONE_NUMBER_ID", "", ""),
            ("DCC_META_WABA_ID", "", ""),
            ("DCC_META_GRAPH_VERSION", "v21.0", ""),
            ("DCC_META_TIMEOUT_SECONDS", "10.0", ""),
            ("DCC_META_SESSION_WINDOW_HOURS", "24", "free-form window; outside it a template"),
        ],
    ),
    (
        "Demo Command Center — NXTutors website gateway",
        [
            ("DCC_GATEWAY_BASE_URL", "", "identity, quotes, activation. Unverified endpoints."),
            ("DCC_WEBSITE_PUBLIC_BASE_URL", "https://www.nxtutors.com", "outbound link allowlist"),
            ("DCC_GATEWAY_AUDIENCE", "demo-command-center", ""),
            ("DCC_GATEWAY_SOURCE_ID", "demo_command_center_agent", ""),
            ("DCC_GATEWAY_SIGNING_KEY_ID", "v1", ""),
            ("DCC_GATEWAY_SIGNING_SECRET", "", "HMAC, same scheme as the Tutor gateway client"),
            ("DCC_GATEWAY_TIMEOUT_SECONDS", "8.0", ""),
            ("DCC_GATEWAY_MAX_RETRIES", "2", ""),
        ],
    ),
    (
        "Demo Command Center — Google Calendar and Meet",
        [
            ("DCC_GOOGLE_ENABLED", "false", ""),
            ("DCC_GOOGLE_AUTH_MODE", "service_account", "service_account | oauth_refresh"),
            ("DCC_GOOGLE_ORGANIZER_EMAIL", "", "mailbox that owns every demo event"),
            ("DCC_GOOGLE_CALENDAR_ID", "primary", ""),
            ("DCC_GOOGLE_CREDENTIALS_SECRET", "", "Secrets Manager name"),
            ("DCC_GOOGLE_TIMEOUT_SECONDS", "12.0", ""),
            ("DCC_GOOGLE_CONFERENCE_POLL_ATTEMPTS", "4", "Meet is created asynchronously"),
            ("DCC_GOOGLE_CONFERENCE_POLL_DELAY_SECONDS", "1.5", ""),
        ],
    ),
    (
        "Demo Command Center — Cashfree",
        [
            ("DCC_CASHFREE_ENABLED", "false", ""),
            ("DCC_CASHFREE_ENV", "sandbox", "sandbox | production"),
            ("DCC_CASHFREE_APP_ID", "", ""),
            ("DCC_CASHFREE_SECRET_KEY", "", "also verifies the webhook signature"),
            ("DCC_CASHFREE_API_VERSION", "2023-08-01", ""),
            ("DCC_CASHFREE_TIMEOUT_SECONDS", "12.0", ""),
            ("DCC_CASHFREE_WEBHOOK_TOLERANCE_SECONDS", "300", "replay window"),
            ("DCC_CASHFREE_RETURN_URL", "", ""),
        ],
    ),
    (
        "Demo Command Center — OpenAI (separate budget from Tutor)",
        [
            ("DCC_LLM_PROVIDER", "stub", "stub | openai. stub is heuristic and offline."),
            ("DCC_OPENAI_API_KEY", "", "may reuse TMM_OPENAI_API_KEY's value"),
            ("DCC_OPENAI_BASE_URL", "", ""),
            ("DCC_MODEL_CLASSIFIER", "gpt-4o-mini", ""),
            ("DCC_MODEL_EXTRACTION", "gpt-4o-mini", ""),
            ("DCC_MODEL_REASONING", "gpt-4o", "objection extraction only"),
            ("DCC_LLM_TIMEOUT_SECONDS", "20.0", ""),
            ("DCC_LLM_MAX_RETRIES", "2", ""),
            ("DCC_LLM_MAX_OUTPUT_TOKENS", "900", ""),
        ],
    ),
    (
        "Demo Command Center — LLM cost ceilings (four, all enforced)",
        [
            ("DCC_LLM_CONVERSATION_TOKEN_BUDGET", "60000", ""),
            ("DCC_LLM_MAX_CALLS_PER_TURN", "2", ""),
            ("DCC_LLM_MAX_CALLS_PER_CONVERSATION", "30", ""),
            ("DCC_LLM_MAX_REASONING_CALLS_PER_CONVERSATION", "3", ""),
            ("DCC_CONTEXT_TOKEN_BUDGET", "4000", ""),
        ],
    ),
    (
        "Demo Command Center — ingress security",
        [
            ("DCC_INTERNAL_SIGNING_SECRET", "", "HMAC for agent-to-agent handoffs"),
            ("DCC_INTERNAL_TIMESTAMP_TOLERANCE_SECONDS", "300", ""),
            ("DCC_MAX_BODY_BYTES", "131072", ""),
            ("DCC_HASH_PEPPER", "", "pseudonymises phone numbers; REQUIRED outside local"),
        ],
    ),
    (
        "Demo Command Center — rate limits",
        [
            ("DCC_RATE_LIMIT_PER_CONVERSATION_PER_MINUTE", "20", ""),
            ("DCC_RATE_LIMIT_PER_IDENTITY_PER_MINUTE", "30", ""),
            ("DCC_RATE_LIMIT_GLOBAL_PER_MINUTE", "2000", ""),
            ("DCC_RATE_LIMIT_LLM_PER_CONVERSATION_PER_MINUTE", "6", ""),
            ("DCC_RATE_LIMIT_WHATSAPP_PER_IDENTITY_PER_HOUR", "20", ""),
            ("DCC_RATE_LIMIT_PAYMENT_ORDERS_PER_CONVERSATION_PER_HOUR", "5", ""),
            ("DCC_RATE_LIMIT_OPS_API_PER_MINUTE", "120", ""),
        ],
    ),
    (
        "Demo Command Center — resilience and cache",
        [
            ("DCC_CIRCUIT_FAILURE_THRESHOLD", "5", ""),
            ("DCC_CIRCUIT_RESET_SECONDS", "60", ""),
            ("DCC_CIRCUIT_HALF_OPEN_PROBES", "1", ""),
            ("DCC_CACHE_DEFAULT_TTL_SECONDS", "300", ""),
            ("DCC_CACHE_AUTHORIZATION_TTL_SECONDS", "60", "bounded exposure after a revoke"),
            ("DCC_CACHE_MAX_ENTRIES", "512", ""),
        ],
    ),
    (
        "Demo Command Center — versioned business policy",
        [
            ("DCC_POLICY_DIR", "config/policies", ""),
            ("DCC_REMINDER_POLICY", "reminder.v1", ""),
            ("DCC_DISCOUNT_POLICY", "discount.v1", ""),
            ("DCC_FORECAST_MODEL", "forecast.v1", ""),
            ("DCC_MONITORING_POLICY", "monitoring.v1", ""),
        ],
    ),
    (
        "Demo Command Center — ownership and handoffs",
        [
            (
                "DCC_OUTBOUND_OWNERSHIP",
                "self_sends",
                "self_sends | caller_sends. Both at once is a double send.",
            ),
            ("DCC_LEAD_INTAKE_WEBHOOK_URL", "", ""),
            ("DCC_ONBOARDING_WEBHOOK_URL", "", "where the converted customer is handed on"),
            ("DCC_HANDOFF_TTL_SECONDS", "3600", ""),
        ],
    ),
    (
        "Demo Command Center — capability kill switches",
        [
            ("DCC_SUPPORTED_LANGUAGES", "en,hi,hinglish", ""),
            ("DCC_DEFAULT_LANGUAGE", "en", ""),
            ("DCC_FLAG_SCHEDULING_ENABLED", "true", ""),
            ("DCC_FLAG_REMINDERS_ENABLED", "true", ""),
            ("DCC_FLAG_PAYMENTS_ENABLED", "true", ""),
            ("DCC_FLAG_DISCOUNTS_ENABLED", "true", ""),
            ("DCC_FLAG_AUTO_FOLLOWUP_ENABLED", "false", ""),
        ],
    ),
]


def existing_keys(text: str) -> set[str]:
    return {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge DCC_* settings into the shared .env")
    parser.add_argument("--write", action="store_true", help="append (default: dry run)")
    args = parser.parse_args()

    if not ENV.is_file():
        print(f"no .env at {ENV}")
        return 1

    text = ENV.read_text(encoding="utf-8")
    present = existing_keys(text)

    lines: list[str] = []
    added = 0
    blank = 0

    header = [
        "",
        "# " + "=" * 74,
        "# DEMO COMMAND CENTER AGENT",
        "#",
        "# The Demo agent reads THIS file. One .env for both agents: two files is",
        "# two places for the database URL to drift apart.",
        "#",
        "# DCC_POSTGRES_DSN is intentionally blank — Demo inherits TMM_POSTGRES_DSN",
        "# above, so the connection string exists in exactly one place. Demo owns a",
        "# separate SCHEMA (DCC_AURORA_SCHEMA=demo_agent); Tutor owns tutor_match.",
        "#",
        "# A blank value means the capability is off. `make doctor` lists them.",
        "# " + "=" * 74,
    ]

    for title, entries in BLOCKS:
        block: list[str] = []
        for key, value, comment in entries:
            if key in present:
                continue
            # A comment goes on its OWN line, never after the value. A dotenv
            # parser takes everything after `=` as the value, `# comment`
            # included — which silently set DCC_POSTGRES_DSN to the comment
            # text and stopped the shared-DSN fallback from ever firing.
            if comment:
                block.append(f"# {comment}")
            block.append(f"{key}={value}")
            added += 1
            if not value:
                blank += 1
        if block:
            lines.extend(["", f"# --- {title} ---", *block])

    if not added:
        print("every DCC_* key is already present; nothing to add.")
        return 0

    print(f"keys to add : {added}  ({blank} intentionally blank)")
    if not args.write:
        print("\n--- DRY RUN ---")
        for line in (*header, *lines):
            print(line)
        print("\nRe-run with --write to append.")
        return 0

    with ENV.open("a", encoding="utf-8") as handle:
        handle.write("\n".join([*header, *lines]) + "\n")
    print(f"appended {added} keys to {ENV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
