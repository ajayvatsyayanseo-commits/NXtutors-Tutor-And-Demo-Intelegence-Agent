-- Demo Command Center — initial schema.
--
-- Lives in its own schema (`demo_agent`) inside the EXISTING cluster. It shares
-- no table with the Tutor Intelligence service (schema `tutor_match`), adds no
-- column to one, and holds no copy of the tutor projection: a tutor reference
-- here is an opaque handle resolved through the website gateway when needed.
--
-- The `dcc_` table prefix predates the dedicated schema and is kept: renaming
-- 36 tables to shave a redundant prefix would touch the repositories, the
-- routing map and the tests for no behavioural gain.
--
-- Conventions, applied without exception:
--   * every timestamp is `timestamptz` and stored in UTC;
--   * a timezone is an IANA name in a `text` column beside the instant;
--   * money is `bigint` minor units (paise) — never numeric, never float;
--   * an identifier a person could be recognised from is an opaque ref;
--   * `jsonb` only where the shape is genuinely open (facts, payloads,
--     feature snapshots), never as a way to avoid designing a column.

CREATE SCHEMA IF NOT EXISTS demo_agent;
SET search_path TO demo_agent;

-- ---------------------------------------------------------------- conversation

CREATE TABLE IF NOT EXISTS dcc_conversations (
    conversation_ref   text PRIMARY KEY,
    tenant_id          text        NOT NULL DEFAULT 'nxtutors',
    owner              text        NOT NULL,
    since              timestamptz NOT NULL,
    lease_expires_at   timestamptz,
    previous           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    last_inbound_at    timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);

-- Ownership leases are swept by a scheduled job; a partial index keeps that
-- scan off the 99% of rows with no lease at all.
CREATE INDEX IF NOT EXISTS dcc_conversations_lease_idx
    ON dcc_conversations (lease_expires_at)
    WHERE lease_expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS dcc_conversation_participants (
    conversation_ref   text        NOT NULL REFERENCES dcc_conversations (conversation_ref) ON DELETE CASCADE,
    party              text        NOT NULL,
    participant_ref    text        NOT NULL,
    display_name       text        NOT NULL DEFAULT '',
    invite_consent     boolean     NOT NULL DEFAULT false,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_ref, party, participant_ref)
);

CREATE TABLE IF NOT EXISTS dcc_conversation_state (
    conversation_ref   text PRIMARY KEY,
    state              text        NOT NULL,
    -- The optimistic lock. Every write asserts the value it read.
    version            bigint      NOT NULL DEFAULT 0,
    demo_id            text,
    facts              jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dcc_conversation_state_state_idx
    ON dcc_conversation_state (state, updated_at DESC);

CREATE TABLE IF NOT EXISTS dcc_state_transitions (
    transition_id      text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    from_state         text        NOT NULL,
    to_state           text        NOT NULL,
    trigger            text        NOT NULL,
    actor              text        NOT NULL,
    command            text        NOT NULL DEFAULT 'none',
    reason             text        NOT NULL DEFAULT '',
    version            bigint      NOT NULL,
    occurred_at        timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS dcc_state_transitions_conversation_idx
    ON dcc_state_transitions (conversation_ref, occurred_at DESC);

-- ------------------------------------------------------------------ ingress

CREATE TABLE IF NOT EXISTS dcc_inbound_events (
    event_id           text        NOT NULL,
    source             text        NOT NULL,
    received_at        timestamptz NOT NULL DEFAULT now(),
    conversation_ref   text,
    payload_digest     text        NOT NULL DEFAULT '',
    PRIMARY KEY (source, event_id)
);

CREATE TABLE IF NOT EXISTS dcc_idempotency_keys (
    scope              text        NOT NULL,
    idempotency_key    text        NOT NULL,
    claimed_at         timestamptz NOT NULL,
    expires_at         timestamptz NOT NULL,
    result             jsonb,
    PRIMARY KEY (scope, idempotency_key)
);

CREATE INDEX IF NOT EXISTS dcc_idempotency_expiry_idx ON dcc_idempotency_keys (expires_at);

CREATE TABLE IF NOT EXISTS dcc_tool_executions (
    execution_id       text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    tool               text        NOT NULL,
    -- What the model proposed and what deterministic validation decided.
    proposal           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    verdict            text        NOT NULL,
    refusal_reason     text        NOT NULL DEFAULT '',
    executed_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dcc_outbox_events (
    outbox_id          text PRIMARY KEY,
    event              text        NOT NULL,
    payload            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    -- Deterministic. The unique index is what makes "wrote the row" and
    -- "published the event" impossible to disagree about.
    idempotency_key    text        NOT NULL UNIQUE,
    created_at         timestamptz NOT NULL DEFAULT now(),
    published_at       timestamptz,
    attempts           integer     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS dcc_outbox_unpublished_idx
    ON dcc_outbox_events (created_at)
    WHERE published_at IS NULL;

-- --------------------------------------------------------------- the demo

CREATE TABLE IF NOT EXISTS dcc_demo_requests (
    request_id         text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    student_ref        text,
    region             text,
    language           text        NOT NULL DEFAULT 'en',
    match_session_id   text,
    created_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dcc_demo_requests_conversation_idx
    ON dcc_demo_requests (conversation_ref, created_at DESC);

CREATE TABLE IF NOT EXISTS dcc_demo_requirements (
    request_id           text PRIMARY KEY REFERENCES dcc_demo_requests (request_id) ON DELETE CASCADE,
    service              text,
    board                text,
    student_class        text,
    subject              text,
    mode                 text,
    region               text,
    locality             text,
    timezone             text        NOT NULL DEFAULT 'Asia/Kolkata',
    availability_note    text,
    special_requirements text,
    updated_at           timestamptz NOT NULL DEFAULT now()
);

-- The snapshot a later tutor selection is validated against. This table is what
-- makes "a tutor ref may only come from options we presented" enforceable.
CREATE TABLE IF NOT EXISTS dcc_tutor_candidate_snapshots (
    snapshot_id        text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    match_session_id   text        NOT NULL,
    rank               integer     NOT NULL,
    tutor_ref          text        NOT NULL,
    display_name       text        NOT NULL,
    profile_url        text        NOT NULL,
    -- Evidence and data quality, kept so a shortlist stays explainable against
    -- the exact ranking run that produced it.
    evidence           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    final_score        double precision,
    weight_coverage    double precision,
    freshness          text        NOT NULL DEFAULT 'unknown',
    captured_at        timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS dcc_candidate_session_rank_uidx
    ON dcc_tutor_candidate_snapshots (conversation_ref, match_session_id, rank);
CREATE INDEX IF NOT EXISTS dcc_candidate_lookup_idx
    ON dcc_tutor_candidate_snapshots (conversation_ref, tutor_ref);

CREATE TABLE IF NOT EXISTS dcc_tutor_confirmation_requests (
    request_id         text PRIMARY KEY,
    demo_id            text        NOT NULL,
    tutor_ref          text        NOT NULL,
    demo_revision      integer     NOT NULL,
    status             text        NOT NULL DEFAULT 'pending',
    template           text        NOT NULL DEFAULT '',
    sent_at            timestamptz,
    responded_at       timestamptz,
    expires_at         timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS dcc_tutor_confirmation_uidx
    ON dcc_tutor_confirmation_requests (demo_id, demo_revision, tutor_ref);

-- The concurrency-safe claim. `conflict_key` is `<tutor_ref>|<minute>`, and the
-- partial unique index means only ONE active hold can exist for it — which is
-- what makes two simultaneous bookings impossible rather than unlikely.
CREATE TABLE IF NOT EXISTS dcc_slot_holds (
    hold_id            text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    tutor_ref          text        NOT NULL,
    conflict_key       text        NOT NULL,
    starts_at          timestamptz NOT NULL,
    duration_minutes   integer     NOT NULL DEFAULT 45,
    timezone           text        NOT NULL DEFAULT 'Asia/Kolkata',
    mode               text        NOT NULL DEFAULT 'online',
    status             text        NOT NULL DEFAULT 'active',
    created_at         timestamptz NOT NULL,
    expires_at         timestamptz NOT NULL,
    resolved_at        timestamptz,
    CONSTRAINT dcc_slot_holds_ttl CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX IF NOT EXISTS dcc_slot_holds_active_uidx
    ON dcc_slot_holds (conflict_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS dcc_slot_holds_expiry_idx
    ON dcc_slot_holds (expires_at) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS dcc_demos (
    demo_id            text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    request_id         text        NOT NULL,
    student_ref        text,
    tutor_ref          text,
    region             text,
    mode               text        NOT NULL DEFAULT 'online',
    language           text        NOT NULL DEFAULT 'en',
    starts_at          timestamptz,
    duration_minutes   integer     NOT NULL DEFAULT 45,
    timezone           text        NOT NULL DEFAULT 'Asia/Kolkata',
    -- The LOGICAL calendar event. A reschedule patches it; it is never
    -- duplicated, which is what stops a parent collecting four invites.
    calendar_event_id  text,
    meet_url           text,
    location_label     text,
    revision           integer     NOT NULL DEFAULT 1,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    cancelled_at       timestamptz,
    cancellation_reason text       NOT NULL DEFAULT '',
    -- An in-person demo with a Meet link means a code path invented one.
    CONSTRAINT dcc_demos_no_meet_for_home CHECK (mode <> 'home' OR meet_url IS NULL),
    CONSTRAINT dcc_demos_meet_needs_event CHECK (meet_url IS NULL OR calendar_event_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS dcc_demos_conversation_idx ON dcc_demos (conversation_ref);
CREATE INDEX IF NOT EXISTS dcc_demos_region_window_idx ON dcc_demos (region, starts_at);
CREATE UNIQUE INDEX IF NOT EXISTS dcc_demos_calendar_event_uidx
    ON dcc_demos (calendar_event_id) WHERE calendar_event_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS dcc_demo_attendees (
    demo_id            text        NOT NULL REFERENCES dcc_demos (demo_id) ON DELETE CASCADE,
    party              text        NOT NULL,
    participant_ref    text        NOT NULL,
    display_name       text        NOT NULL DEFAULT '',
    invite_consent     boolean     NOT NULL DEFAULT false,
    PRIMARY KEY (demo_id, party)
);

CREATE TABLE IF NOT EXISTS dcc_demo_reminders (
    reminder_id        text PRIMARY KEY,
    demo_id            text        NOT NULL REFERENCES dcc_demos (demo_id) ON DELETE CASCADE,
    conversation_ref   text        NOT NULL,
    -- Behind the demo's revision = obsolete, with no join required.
    demo_revision      integer     NOT NULL,
    label              text        NOT NULL,
    audience           text        NOT NULL,
    recipient_ref      text        NOT NULL,
    template           text        NOT NULL,
    channel            text        NOT NULL DEFAULT 'whatsapp',
    fire_at            timestamptz NOT NULL,
    demo_starts_at     timestamptz NOT NULL,
    status             text        NOT NULL DEFAULT 'pending',
    attempts           integer     NOT NULL DEFAULT 0,
    sent_at            timestamptz,
    suppression_reason text        NOT NULL DEFAULT ''
);

-- One reminder per (demo, revision, label, audience). A reschedule bumps the
-- revision and therefore legitimately gets a fresh ladder.
CREATE UNIQUE INDEX IF NOT EXISTS dcc_demo_reminders_uidx
    ON dcc_demo_reminders (demo_id, demo_revision, label, audience);
CREATE INDEX IF NOT EXISTS dcc_demo_reminders_due_idx
    ON dcc_demo_reminders (fire_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS dcc_demo_outcomes (
    demo_id            text PRIMARY KEY REFERENCES dcc_demos (demo_id) ON DELETE CASCADE,
    outcome            text        NOT NULL DEFAULT 'unknown',
    student_attended   boolean,
    tutor_attended     boolean,
    -- How we know. The state machine refuses a no-show whose source is 'llm'.
    evidence_source    text        NOT NULL DEFAULT 'none',
    duration_minutes   integer,
    recorded_by        text        NOT NULL DEFAULT '',
    notes              text        NOT NULL DEFAULT '',
    recorded_at        timestamptz
);

CREATE TABLE IF NOT EXISTS dcc_no_show_events (
    event_id           text PRIMARY KEY,
    demo_id            text        NOT NULL,
    party              text        NOT NULL,
    evidence_source    text        NOT NULL,
    grace_minutes      integer     NOT NULL DEFAULT 0,
    resolved           boolean     NOT NULL DEFAULT false,
    resolved_by        text        NOT NULL DEFAULT '',
    occurred_at        timestamptz NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------- analysis

CREATE TABLE IF NOT EXISTS dcc_objection_analyses (
    demo_id            text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    objections         jsonb       NOT NULL DEFAULT '[]'::jsonb,
    sentiment          text        NOT NULL DEFAULT 'neutral',
    intent             text        NOT NULL DEFAULT 'unknown',
    recommended_next_step text     NOT NULL DEFAULT 'none',
    summary            text        NOT NULL DEFAULT '',
    model_ref          text        NOT NULL DEFAULT '',
    prompt_version     text        NOT NULL DEFAULT '',
    -- Quotes the model produced that were not in the transcript. Never empty
    -- silently: this is the metric that catches drift.
    fabricated_quotes  jsonb       NOT NULL DEFAULT '[]'::jsonb,
    analysed_at        timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS dcc_forecast_versions (
    policy_stamp       text PRIMARY KEY,
    policy_id          text        NOT NULL,
    version            text        NOT NULL,
    intercept          double precision NOT NULL,
    features           jsonb       NOT NULL,
    first_seen_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dcc_conversion_forecasts (
    forecast_id        text PRIMARY KEY,
    demo_id            text        NOT NULL,
    probability        double precision NOT NULL,
    risk_band          text        NOT NULL,
    confidence         text        NOT NULL,
    strategy           text        NOT NULL,
    -- Reproducing the score requires nothing but these three columns.
    features           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    missing_features   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    contributions      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    policy_stamp       text        NOT NULL,
    scored_at          timestamptz NOT NULL,
    CONSTRAINT dcc_forecast_probability_range CHECK (probability >= 0 AND probability <= 1)
);

CREATE INDEX IF NOT EXISTS dcc_conversion_forecasts_demo_idx
    ON dcc_conversion_forecasts (demo_id, scored_at DESC);

CREATE TABLE IF NOT EXISTS dcc_demo_quality_scores (
    demo_id            text        NOT NULL,
    score              double precision NOT NULL,
    computed_at        timestamptz NOT NULL,
    PRIMARY KEY (demo_id, computed_at),
    CONSTRAINT dcc_quality_range CHECK (score >= 0 AND score <= 1)
);

-- -------------------------------------------------------------- commerce

CREATE TABLE IF NOT EXISTS dcc_discount_decisions (
    decision_id        text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    demo_id            text        NOT NULL,
    student_ref        text,
    status             text        NOT NULL,
    band_name          text        NOT NULL DEFAULT '',
    percent            integer     NOT NULL DEFAULT 0,
    list_price_minor   bigint      NOT NULL,
    discount_minor     bigint      NOT NULL DEFAULT 0,
    payable_minor      bigint      NOT NULL,
    floor_minor        bigint      NOT NULL,
    currency           text        NOT NULL DEFAULT 'INR',
    triggers           jsonb       NOT NULL DEFAULT '[]'::jsonb,
    conditions         jsonb       NOT NULL DEFAULT '[]'::jsonb,
    reason_code        text        NOT NULL DEFAULT 'none',
    requires_human_approval boolean NOT NULL DEFAULT false,
    approved_by        text        NOT NULL DEFAULT '',
    -- The exact policy bytes. "Why did this customer get 15% off" is
    -- answerable six months later without reading today's source.
    policy_stamp       text        NOT NULL,
    valid_until        timestamptz,
    decided_at         timestamptz NOT NULL,
    -- The arithmetic must balance in the row, not only in the engine.
    CONSTRAINT dcc_discount_balances CHECK (list_price_minor - discount_minor = payable_minor),
    CONSTRAINT dcc_discount_floor CHECK (status <> 'approved' OR payable_minor >= floor_minor),
    CONSTRAINT dcc_discount_percent CHECK (percent >= 0 AND percent <= 100)
);

CREATE UNIQUE INDEX IF NOT EXISTS dcc_discount_demo_uidx ON dcc_discount_decisions (demo_id);
CREATE INDEX IF NOT EXISTS dcc_discount_customer_idx
    ON dcc_discount_decisions (student_ref, decided_at DESC) WHERE student_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS dcc_payment_orders (
    order_ref          text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    demo_id            text        NOT NULL,
    student_ref        text,
    amount_minor       bigint      NOT NULL,
    currency           text        NOT NULL DEFAULT 'INR',
    status             text        NOT NULL DEFAULT 'created',
    provider_order_id  text        NOT NULL DEFAULT '',
    payment_link       text        NOT NULL DEFAULT '',
    -- What authorised this amount. Never inferred later.
    offer_policy_stamp text        NOT NULL,
    discount_percent   integer     NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL,
    expires_at         timestamptz NOT NULL,
    paid_at            timestamptz,
    CONSTRAINT dcc_order_amount_positive CHECK (amount_minor > 0)
);

CREATE INDEX IF NOT EXISTS dcc_payment_orders_conversation_idx
    ON dcc_payment_orders (conversation_ref, created_at DESC);

CREATE TABLE IF NOT EXISTS dcc_payment_events (
    -- The provider's own event id. This primary key IS the replay defence.
    provider_event_id  text PRIMARY KEY,
    order_ref          text        NOT NULL,
    kind               text        NOT NULL,
    amount_minor       bigint      NOT NULL,
    currency           text        NOT NULL DEFAULT 'INR',
    provider_reference text        NOT NULL DEFAULT '',
    signature_verified boolean     NOT NULL DEFAULT false,
    raw_digest         text        NOT NULL DEFAULT '',
    occurred_at        timestamptz NOT NULL,
    recorded_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dcc_payment_events_order_idx ON dcc_payment_events (order_ref);

CREATE TABLE IF NOT EXISTS dcc_subscription_activation_attempts (
    attempt_id         text PRIMARY KEY,
    order_ref          text        NOT NULL,
    conversation_ref   text        NOT NULL,
    -- Deterministic per order, so every retry presents the same key and the
    -- gateway's idempotency actually does something.
    idempotency_key    text        NOT NULL,
    attempt            integer     NOT NULL DEFAULT 1,
    succeeded          boolean     NOT NULL DEFAULT false,
    subscription_ref   text        NOT NULL DEFAULT '',
    error_code         text        NOT NULL DEFAULT '',
    attempted_at       timestamptz NOT NULL,
    CONSTRAINT dcc_activation_ref CHECK (NOT succeeded OR subscription_ref <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS dcc_activation_order_attempt_uidx
    ON dcc_subscription_activation_attempts (order_ref, attempt);
-- At most one successful activation per order, forever.
CREATE UNIQUE INDEX IF NOT EXISTS dcc_activation_success_uidx
    ON dcc_subscription_activation_attempts (order_ref) WHERE succeeded;

-- ------------------------------------------------------------- handoffs

CREATE TABLE IF NOT EXISTS dcc_handoffs (
    handoff_id         text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    source_agent       text        NOT NULL,
    destination_agent  text        NOT NULL,
    event_type         text        NOT NULL,
    idempotency_key    text        NOT NULL UNIQUE,
    trace_id           text        NOT NULL DEFAULT '',
    correlation_id     text        NOT NULL DEFAULT '',
    causation_id       text,
    hop_count          integer     NOT NULL DEFAULT 0,
    status             text        NOT NULL DEFAULT 'requested',
    occurred_at        timestamptz NOT NULL DEFAULT now(),
    expires_at         timestamptz
);

CREATE TABLE IF NOT EXISTS dcc_human_handoff_cases (
    case_id            text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    demo_id            text,
    state              text        NOT NULL,
    reason             text        NOT NULL,
    severity           text        NOT NULL DEFAULT 'normal',
    assigned_to        text        NOT NULL DEFAULT '',
    resolved_at        timestamptz,
    opened_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dcc_human_cases_open_idx
    ON dcc_human_handoff_cases (opened_at DESC) WHERE resolved_at IS NULL;

-- ----------------------------------------------------------- messaging

CREATE TABLE IF NOT EXISTS dcc_message_log (
    -- Deterministic per business action. The exclusion that makes duplicate
    -- sends impossible rather than unlikely.
    idempotency_key    text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    recipient_ref      text        NOT NULL,
    kind               text        NOT NULL,
    template           text        NOT NULL DEFAULT '',
    demo_id            text,
    outcome            text        NOT NULL DEFAULT 'claimed',
    provider_message_id text       NOT NULL DEFAULT '',
    delivery_status    text        NOT NULL DEFAULT '',
    detail             text        NOT NULL DEFAULT '',
    claimed_at         timestamptz NOT NULL DEFAULT now(),
    sent_at            timestamptz
);

CREATE INDEX IF NOT EXISTS dcc_message_log_recipient_idx
    ON dcc_message_log (recipient_ref, claimed_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS dcc_message_log_provider_uidx
    ON dcc_message_log (provider_message_id) WHERE provider_message_id <> '';

CREATE TABLE IF NOT EXISTS dcc_message_summaries (
    conversation_ref   text        NOT NULL,
    summary_id         text        NOT NULL,
    -- Privacy-safe rolling summary. Redacted before storage; never a transcript.
    summary            text        NOT NULL,
    turn_count         integer     NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_ref, summary_id)
);

-- ------------------------------------------------------------ operations

CREATE TABLE IF NOT EXISTS dcc_provider_failure_state (
    provider           text PRIMARY KEY,
    consecutive_failures integer   NOT NULL DEFAULT 0,
    circuit_state      text        NOT NULL DEFAULT 'closed',
    opened_at          timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dcc_regional_metric_rollups (
    rollup_id          text PRIMARY KEY,
    region             text        NOT NULL,
    metric             text        NOT NULL,
    value              double precision,
    sample_size        integer     NOT NULL DEFAULT 0,
    window_start       timestamptz NOT NULL,
    window_end         timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS dcc_rollup_window_uidx
    ON dcc_regional_metric_rollups (region, metric, window_start);

CREATE TABLE IF NOT EXISTS dcc_underperformance_alerts (
    alert_id           text PRIMARY KEY,
    region             text        NOT NULL,
    rule               text        NOT NULL,
    metric             text        NOT NULL,
    value              double precision NOT NULL,
    threshold          double precision NOT NULL,
    sample_size        integer     NOT NULL,
    severity           text        NOT NULL DEFAULT 'warning',
    fired_at           timestamptz NOT NULL DEFAULT now()
);

-- The cooldown lookup: "when did this rule last fire for this region".
CREATE INDEX IF NOT EXISTS dcc_alerts_cooldown_idx
    ON dcc_underperformance_alerts (region, rule, fired_at DESC);

CREATE TABLE IF NOT EXISTS dcc_prompt_versions (
    prompt_version     text PRIMARY KEY,
    purpose            text        NOT NULL,
    checksum           text        NOT NULL,
    first_seen_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dcc_model_usage (
    usage_id           text PRIMARY KEY,
    conversation_ref   text        NOT NULL,
    purpose            text        NOT NULL,
    model_ref          text        NOT NULL,
    input_tokens       integer     NOT NULL DEFAULT 0,
    output_tokens      integer     NOT NULL DEFAULT 0,
    estimated_cost_micros bigint   NOT NULL DEFAULT 0,
    occurred_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dcc_model_usage_conversation_idx
    ON dcc_model_usage (conversation_ref, occurred_at DESC);

CREATE TABLE IF NOT EXISTS dcc_audit_events (
    audit_id           text PRIMARY KEY,
    conversation_ref   text,
    event              text        NOT NULL,
    actor              text        NOT NULL DEFAULT '',
    detail             jsonb       NOT NULL DEFAULT '{}'::jsonb,
    occurred_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS dcc_audit_events_conversation_idx
    ON dcc_audit_events (conversation_ref, occurred_at DESC);
