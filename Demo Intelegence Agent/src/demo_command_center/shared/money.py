"""Money as integer minor units.

Every rupee amount in this system — plan price, discount, Cashfree order,
webhook verification — is an `int` count of paise. Floats are banned: the
discount engine compares an amount we computed against an amount Cashfree
reports, and `0.1 + 0.2 != 0.3` turns that equality check into an intermittent
payment-verification failure that only shows up for some price points.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

INR: Final = "INR"
_MINOR_UNITS: Final = 100


class CurrencyMismatch(ValueError):
    def __init__(self, left: str, right: str) -> None:
        super().__init__(f"cannot combine {left} and {right}")


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """An exact amount. `minor` is paise for INR."""

    minor: int
    currency: str = INR

    def __post_init__(self) -> None:
        if self.minor < 0:
            raise ValueError("Money cannot be negative; model a discount as a separate amount")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise ValueError(f"currency must be an ISO-4217 code, got {self.currency!r}")

    @classmethod
    def from_major(cls, amount: str | int | Decimal, currency: str = INR) -> Money:
        """From a rupee figure. Accepts str/int/Decimal — never float."""
        quantised = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return cls(int(quantised * _MINOR_UNITS), currency)

    @property
    def major(self) -> Decimal:
        return (Decimal(self.minor) / _MINOR_UNITS).quantize(Decimal("0.01"))

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        if other.minor > self.minor:
            raise ValueError("subtraction would produce a negative amount")
        return Money(self.minor - other.minor, self.currency)

    def percent(self, pct: Decimal | str | int) -> Money:
        """A percentage of this amount, rounded half-up to the paise.

        Rounding is pinned because the discounted price we quote in WhatsApp and
        the order amount we send to Cashfree must be byte-identical; two
        different roundings is a mismatch the webhook verifier will reject.
        """
        factor = Decimal(str(pct)) / Decimal(100)
        return Money(
            int((Decimal(self.minor) * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)),
            self.currency,
        )

    def display(self) -> str:
        """`₹1,200.00` — what a parent sees. Never used for comparison."""
        return f"₹{self.major:,.2f}"

    def __str__(self) -> str:
        return f"{self.major} {self.currency}"
