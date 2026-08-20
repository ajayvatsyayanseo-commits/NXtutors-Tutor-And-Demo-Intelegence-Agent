"""Every approved template must survive the whole outbound boundary.

The bug this file exists to prevent shipped once and was invisible: the output
guard flagged `dmo_...` as an internal reference leak, and every approved
template renders that id in a literal "Reference:" field. So all three
reminders, the tutor confirmation, the tutor-expiry notice and the scheduled
confirmation were refused at send time.

Nothing failed loudly. The lifecycle passed every step, the state machine
advanced, the reminder rows were marked, and no message arrived. That is the
worst shape a defect can take in this system, so it gets a test that walks the
real registry rather than a hand-listed set — a template added later is covered
the day it is added.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from demo_command_center.contracts.common import Language, Party
from demo_command_center.domain.messages import (
    MessageKind,
    OutboundMessage,
    TemplateBinding,
)
from demo_command_center.guardrails.output import (
    OutputGuard,
    customer_safe_url_policy,
)
from demo_command_center.integrations.meta_whatsapp.templates import registry

pytestmark = pytest.mark.security

#: What the senders actually build. Values mirror `_reminder_variables` in
#: handlers/workers.py and `window_safe_template` in orchestration/composer.py.
SAMPLE: dict[str, str] = {
    "demo_datetime": "Tue 18 Aug, 5:00 PM",
    "timezone": "Asia/Kolkata",
    "reference": "dmo_01M09HA0EV4TQE0D3JB2FP9AAW",
    "join_link": "https://meet.google.com/fix-0001-abc",
    "student_name": "there",
    "tutor_name": "Arjun Desai",
}


def guard() -> OutputGuard:
    return OutputGuard(customer_safe_url_policy(website_host="nxtutors.example"))


def message_for(name: str) -> OutboundMessage:
    template = registry().get(name)
    return OutboundMessage(
        conversation_ref="cv_guard",
        recipient_ref="cv_guard",
        audience=Party.STUDENT,
        kind=MessageKind.REMINDER,
        language=Language.EN,
        template=TemplateBinding(
            name=name,
            language="en",
            variables=tuple(SAMPLE.get(v, "-") for v in template.variables),
        ),
        idempotency_key=f"k_{name}",
        created_at=datetime.now(UTC),
    )


APPROVED = sorted(registry().approved_names())


class TestEveryApprovedTemplateSurvivesTheGuard:
    @pytest.mark.parametrize("name", APPROVED)
    def test_it_is_not_blocked(self, name: str) -> None:
        verdict = guard().check(message_for(name))
        assert verdict.allowed, f"{name} would never be delivered: {verdict.violations}"

    def test_the_registry_is_not_empty(self) -> None:
        """Guards the parametrisation above: an empty registry would make every
        test in this class vacuously pass."""
        assert len(APPROVED) >= 6

    @pytest.mark.parametrize("name", APPROVED)
    def test_no_variable_is_blank(self, name: str) -> None:
        """Meta rejects an empty parameter, and a blank renders a visible gap."""
        for value in message_for(name).template.variables:  # type: ignore[union-attr]
            assert value.strip()


class TestTheReferenceRuleIsNarrow:
    """`dmo_` is allowed. Nothing else is."""

    def test_the_customer_facing_demo_reference_is_allowed(self) -> None:
        assert guard().check(message_for("demo_reminder_t24h")).allowed

    @pytest.mark.parametrize(
        "leaked",
        ["cv_abc123456", "hld_01JXYZ789", "ph_deadbeef99", "rmd_aabbccdd", "nxo_zz9988776"],
    )
    def test_internal_references_are_still_blocked(self, leaked: str) -> None:
        message = OutboundMessage(
            conversation_ref="cv_guard",
            recipient_ref="cv_guard",
            audience=Party.STUDENT,
            kind=MessageKind.CONFIRMATION,
            language=Language.EN,
            body=f"Your booking {leaked} is confirmed.",
            idempotency_key="k_leak",
            created_at=datetime.now(UTC),
        )
        verdict = guard().check(message)
        assert not verdict.allowed
        assert "internal_reference_leaked" in verdict.violations

    def test_the_pattern_still_has_its_word_boundaries(self) -> None:
        """A regex written through a shell heredoc once became `\x08(?:...)` —
        a literal backspace — which matched nothing and silently disabled the
        whole rule while still looking right in `grep` output."""
        from demo_command_center.guardrails.output import _REF_PATTERN

        assert _REF_PATTERN.pattern.startswith(chr(92) + "b"), (
            f"word boundary is not a literal backslash-b: {_REF_PATTERN.pattern[:4]!r}"
        )
        assert _REF_PATTERN.search("cv_abc123456") is not None


class TestTheReminderLadderIsDeliverable:
    """The three reminders are the templates most likely to fail unnoticed:
    they fire hours after anyone is watching."""

    @pytest.mark.parametrize(
        "name", ["demo_reminder_t24h", "demo_reminder_t2h", "demo_reminder_t15m"]
    )
    def test_each_rung_binds_and_passes(self, name: str) -> None:
        template = registry().get(name)
        assert template.approved
        assert guard().check(message_for(name)).allowed

    def test_the_policy_names_only_templates_that_exist(self) -> None:
        """A policy edit that renames a template silently disables that rung."""
        import yaml

        from demo_command_center.config.settings import get_settings

        path = __import__("pathlib").Path(get_settings().policy_dir) / "reminder.v1.yaml"
        if not path.is_file():  # policy dir differs under some runners
            pytest.skip(f"reminder policy not at {path}")
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for offset in document["offsets"]:
            assert offset["template"] in registry().approved_names(), (
                f"policy offset {offset['label']} names {offset['template']}, "
                "which is not an approved template"
            )
