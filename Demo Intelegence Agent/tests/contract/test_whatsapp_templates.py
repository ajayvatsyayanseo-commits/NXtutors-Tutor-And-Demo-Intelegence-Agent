"""The template registry pinned to what Meta actually approved.

Read off the WABA template editor on 2026-08-18. These tests exist because the
failure they guard is invisible everywhere else:

* An **arity** mismatch is refused at send time. The reminder simply never
  arrives, fifteen minutes before a demo, and the only trace is a log line.
* An **order** mismatch is worse. Meta binds `{{1}}..{{n}}` positionally, so the
  right count in the wrong order delivers happily and renders
  `Date and time: Rahul` / `Time zone: Thu 21 Aug, 10:00 AM`.

Both were live in the registry before this file existed.
"""

from __future__ import annotations

import pytest

from demo_command_center.integrations.meta_whatsapp.templates import (
    TEMPLATE_CANCELLED,
    TEMPLATE_FOLLOWUP,
    TEMPLATE_REMINDER_T2H,
    TEMPLATE_REMINDER_T15M,
    TEMPLATE_REMINDER_T24H,
    TEMPLATE_SCHEDULED_CONFIRMATION,
    TEMPLATE_TUTOR_CONFIRMATION,
    TEMPLATE_TUTOR_REQUEST_EXPIRED,
    TemplateNotApproved,
    registry,
)

pytestmark = pytest.mark.contract

#: name -> (ordered variables, approved languages) exactly as the WABA holds it.
APPROVED: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    TEMPLATE_REMINDER_T24H: (("demo_datetime", "timezone", "reference"), frozenset({"en"})),
    TEMPLATE_REMINDER_T2H: (("demo_datetime", "timezone", "reference"), frozenset({"en"})),
    TEMPLATE_REMINDER_T15M: (("demo_datetime", "timezone", "reference"), frozenset({"en"})),
    TEMPLATE_TUTOR_REQUEST_EXPIRED: (("reference",), frozenset({"en"})),
    TEMPLATE_SCHEDULED_CONFIRMATION: (
        ("demo_datetime", "timezone", "join_link", "reference"),
        frozenset({"en"}),
    ),
    # Confirmed 2026-08-18 from the editor, id 2056157498358027. `_requested`,
    # not `_request`: the list view truncated it and the obvious guess was wrong.
    TEMPLATE_TUTOR_CONFIRMATION: (
        ("demo_datetime", "timezone", "reference"),
        frozenset({"en"}),
    ),
    # Both went Active between submission and 2026-08-18. Verified directly
    # against GET /{waba}/message_templates: status APPROVED, language en,
    # arity 3 and 1 respectively — matching what was pinned before approval.
    TEMPLATE_CANCELLED: (("demo_datetime", "timezone", "reference"), frozenset({"en"})),
    TEMPLATE_FOLLOWUP: (("reference",), frozenset({"en"})),
}

#: Nothing is awaiting review. Kept as an explicit empty map rather than
#: deleted: the refusal mechanism below is what makes it safe to declare a
#: template before Meta approves it, and it must keep working for the next one.
PENDING_REVIEW: dict[str, tuple[str, ...]] = {}


@pytest.mark.parametrize("name", sorted(APPROVED))
def test_variable_order_matches_the_approved_template(name: str) -> None:
    expected, _ = APPROVED[name]
    assert registry().get(name).variables == expected


@pytest.mark.parametrize("name", sorted(APPROVED))
def test_languages_match_the_approval(name: str) -> None:
    """Approved in English only. Declaring `hi` here makes `bind()` accept a
    language Meta rejects at send time."""
    _, languages = APPROVED[name]
    assert registry().get(name).languages == languages


@pytest.mark.parametrize("name", sorted(APPROVED))
def test_binding_with_the_declared_arity_succeeds(name: str) -> None:
    expected, _ = APPROVED[name]
    binding = registry().bind(
        name, language="en", variables=tuple(f"v{i}" for i in range(len(expected)))
    )
    assert binding.name == name
    assert len(binding.variables) == len(expected)


@pytest.mark.parametrize("name", sorted(APPROVED))
def test_binding_with_the_wrong_arity_is_refused(name: str) -> None:
    expected, _ = APPROVED[name]
    with pytest.raises(TemplateNotApproved):
        registry().bind(name, language="en", variables=("only-one",) * (len(expected) + 1))


@pytest.mark.parametrize("name", sorted(APPROVED))
def test_an_empty_variable_is_refused(name: str) -> None:
    """Meta rejects an empty parameter, and a blank shifts nothing — it just
    renders a gap where the date should be."""
    expected, _ = APPROVED[name]
    with pytest.raises(TemplateNotApproved):
        registry().bind(name, language="en", variables=("",) * len(expected))


def test_no_approved_template_carries_a_personal_name() -> None:
    """The approved set is deliberately name-free. If a personalised variant is
    approved later this fails, which is the prompt to re-read the editor rather
    than assume the position."""
    for name in APPROVED:
        variables = registry().get(name).variables
        assert "student_name" not in variables
        assert "tutor_name" not in variables


class TestTheUnapprovedRefusal:
    """A declared-but-unapproved template must be refused, never optimistically
    used. Nothing is pending today, so this exercises the mechanism against a
    synthetic entry — otherwise the guard would go untested the moment the last
    real template went Active, and silently rot before the next one is added.
    """

    def _registry_with_a_pending_template(self):  # type: ignore[no-untyped-def]
        from demo_command_center.integrations.meta_whatsapp.templates import (
            Template,
            TemplateRegistry,
        )

        pending = Template(
            name="demo_not_yet_active",
            languages=frozenset({"en"}),
            variables=("reference",),
            approved=False,
            note="Submitted, awaiting review.",
        )
        return TemplateRegistry((pending,)), pending

    def test_binding_is_refused(self) -> None:
        registry_, pending = self._registry_with_a_pending_template()
        with pytest.raises(TemplateNotApproved):
            registry_.bind(pending.name, language="en", variables=("dmo_1",))

    def test_excluded_from_the_outbound_allowlist(self) -> None:
        registry_, pending = self._registry_with_a_pending_template()
        assert pending.name not in registry_.approved_names()

    def test_the_doctor_reports_it(self) -> None:
        registry_, pending = self._registry_with_a_pending_template()
        assert pending.name in {t.name for t in registry_.unconfirmed()}

    def test_nothing_real_is_awaiting_review(self) -> None:
        """If a template is added and submitted, add it to PENDING_REVIEW so its
        shape is pinned before approval rather than read under time pressure."""
        assert {t.name for t in registry().unconfirmed()} == set(PENDING_REVIEW)


def test_the_tutor_confirmation_name_is_exact() -> None:
    """`_requested`, not `_request`. The list view truncates at
    `demo_tutor_confirmation_rec…`, and the plausible guess is the wrong one —
    which Meta rejects at send time, leaving a tutor unaware of a booking."""
    assert TEMPLATE_TUTOR_CONFIRMATION == "demo_tutor_confirmation_requested"


def test_every_reminder_offset_uses_an_approved_template() -> None:
    """The policy names templates as strings. A typo there disables that rung of
    the ladder silently."""
    from demo_command_center.bootstrap import reminder_policy
    from demo_command_center.config.settings import get_settings

    approved = registry().approved_names()
    for offset in reminder_policy(get_settings()).offsets:
        assert offset.template in approved, f"{offset.label} names an unapproved template"
