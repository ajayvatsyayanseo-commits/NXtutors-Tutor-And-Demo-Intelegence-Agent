"""Output guardrails — the last check before a message leaves the system.

Input guardrails (`security/guardrails.py`) stop hostile text reaching a model.
These stop *our own* output reaching a parent when it should not. The two failure
modes are different and both are real:

* **Leaking.** An internal reason code, a stack fragment, a pseudonymous ref or
  someone else's phone number ending up in a WhatsApp bubble.
* **Fabricating.** A URL nobody verified, or a claim with no fact behind it.
  The Meet link check is the sharpest version: the only host allowed in an
  outbound message is one on the allowlist, so a link injected through a tutor
  profile field cannot be forwarded to a customer.

A blocked message is never partially sent. `check()` returns the message to send
or refuses outright — there is no "sanitise and hope" path, because a message
that needed sanitising was built wrong and the bug should be visible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from demo_command_center.domain.messages import OutboundMessage
from demo_command_center.security.pii import found_pii_kinds
from demo_command_center.security.urls import UrlPolicy, is_allowed

#: Internal vocabulary that must never appear in a customer-facing message.
#: Matched case-insensitively as whole tokens.
_INTERNAL_TOKENS: tuple[str, ...] = (
    "conversation_ref",
    "student_ref",
    "tutor_ref",
    "idempotency",
    "traceback",
    "exception",
    "null",
    "undefined",
    "none",
    "policy_stamp",
    "data_quality",
    "weight_coverage",
    "replacement_risk",
    "final_score",
)

#: Opaque refs and hashes. `cv_a1b2...`, `hld_01J...`, a bare 32-hex digest.
#:
#: `dmo_` is deliberately ABSENT. The demo id is the one reference in this
#: system that is customer-facing by design: it is printed on the calendar
#: invite the parent already holds, and every approved WhatsApp template
#: renders it in a literal "Reference:" field so a parent can quote it to
#: support and resolve to exactly one demo.
#:
#: Including it here blocked **every template in the registry** - all three
#: reminders, the tutor confirmation, the tutor-expiry notice and the scheduled
#: confirmation - at the output guard, which is the quietest place in the
#: system to fail: the send is refused, the reminder never arrives, and nothing
#: turns red. The other prefixes stay: a conversation ref, a phone hash, a slot
#: hold and an order id are ours, not the customer's.
_REF_PATTERN = re.compile(r"\b(?:cv_|ph_|ct_|hld_|nxo_|rmd_)[A-Za-z0-9]{6,}\b")
_HEX_PATTERN = re.compile(r"\b[0-9a-f]{24,}\b")
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
#: Unrendered template placeholders. `{{1}}` reaching a parent means a variable
#: was never bound, which is a visible bug in every message of that kind.
_PLACEHOLDER = re.compile(r"\{\{\s*\d+\s*\}\}|\{[a-z_]+\}")


@dataclass(frozen=True, slots=True)
class GuardResult:
    message: OutboundMessage
    violations: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.violations


class OutputGuard:
    def __init__(self, url_policy: UrlPolicy | None = None) -> None:
        self._urls = url_policy or UrlPolicy()

    def check(self, message: OutboundMessage) -> GuardResult:
        """Every violation, not just the first — an operator needs the set."""
        violations: list[str] = []
        body = message.body

        if body:
            violations.extend(self._check_text(body))
        for button in message.buttons:
            violations.extend(f"button:{v}" for v in self._check_text(button.title))
        if message.template is not None:
            for index, variable in enumerate(message.template.variables, start=1):
                violations.extend(f"template_var_{index}:{v}" for v in self._check_text(variable))
            if not message.template.name.strip():
                violations.append("empty_template_name")

        return GuardResult(message=message, violations=tuple(dict.fromkeys(violations)))

    def _check_text(self, text: str) -> list[str]:
        problems: list[str] = []

        if _PLACEHOLDER.search(text):
            problems.append("unrendered_placeholder")

        # PII in an outbound message is almost always someone *else's* — the
        # recipient's own number arrives via the resolver, not the body.
        #
        # URLs are excluded from the PII verdict: `redact()` treats a URL as an
        # identifier because a link can carry a token, but an outbound message
        # is *supposed* to contain links (Meet, profile, payment). The allowlist
        # check below is the real control, and counting a link as PII here
        # blocked every legitimate message.
        problems.extend(f"pii:{kind}" for kind in found_pii_kinds(text) if kind != "url")

        if _REF_PATTERN.search(text):
            problems.append("internal_reference_leaked")
        if _HEX_PATTERN.search(text):
            problems.append("hash_leaked")

        lowered = text.lower()
        for token in _INTERNAL_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                problems.append(f"internal_token:{token}")

        for url in _URL_PATTERN.findall(text):
            if not is_allowed(url.rstrip(".,;:)"), self._urls):
                problems.append("unapproved_url")
                break

        return problems


def customer_safe_url_policy(*, website_host: str = "") -> UrlPolicy:
    """The allowlist for links we put in front of a parent.

    Narrower than the SSRF policy: this is what a customer may be *sent*, which
    is Meet, the Cashfree hosted page, and the NXTutors site. Notably absent is
    `graph.facebook.com` — we call it, we never link to it.
    """
    hosts = {"meet.google.com", "payments.cashfree.com", "payments-test.cashfree.com"}
    if website_host:
        hosts.add(website_host.lower())
    return UrlPolicy(allowed_hosts=frozenset(hosts))
