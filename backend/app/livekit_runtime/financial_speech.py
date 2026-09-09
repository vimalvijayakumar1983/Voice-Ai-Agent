"""Presentation-only money formatting; source figures and identifiers never change.

Only explicit currency-labelled amounts backed by typed monetary evidence qualify.
This is not a number scrubber: dates, quantities, IDs and unlabelled values stay put.
The final spoken text still needs semantic verification against the cited sources.
"""

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.livekit_runtime.report_presentation import CURRENCIES, EXACT, SCALES

MONEY_FIELDS = {
    "revenue_ex_vat",
    "total_revenue_ex_vat",
    "total_across_returned_groups_ex_vat",
    "sales_amount",
    "purchase_amount",
    "total_purchase_amount",
    "receivable_amount",
    "outstanding_amount",
    "amount_due",
    "invoice_amount",
    "payment_amount",
}
EXACT_FIELDS = {"amount_due", "invoice_amount", "payment_amount"}
CURRENCY_NAMES = {name.lower(): code for code, name in CURRENCIES.items()}
CURRENCY_NAMES.update({code.lower(): code for code in CURRENCIES})
CURRENCY_NAMES["dollars"] = "USD"
_currency = "|".join(re.escape(s) for s in sorted(CURRENCY_NAMES, key=len, reverse=True))
_number = r"-?\d[\d,]*(?:\.\d+)?"
MONEY = re.compile(
    rf"(?<!\w)(?:(?P<before>{_currency})\s+(?P<left>{_number})(?!\w|[.,]\d)"
    rf"|(?P<right>{_number})\s+(?P<after>{_currency})(?!\w))",
    re.I,
)


def summary_amount(value):
    """Decimal half-up at two places in the selected spoken unit."""
    divisor, unit = (
        (Decimal(1000000), "million") if abs(value) >= 1000000 else (Decimal(1000), "thousand")
    )
    if abs(value) < 1000:
        return format(value, ",f")
    rounded = (value / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    # Do not say '1000 thousand' at the million boundary.
    if unit == "thousand" and abs(rounded) >= 1000:
        return summary_amount(rounded * divisor)
    text = format(rounded, ",.2f")
    if unit == "thousand":
        text = text.rstrip("0").rstrip(".")
    return f"{text} {unit}"


def monetary_values(packets):
    """Return summary candidates and protected exact amounts, keyed by currency.

    Unknown units/fields deliberately do not acquire a magnitude or monetary meaning.
    Generic JSON reports are supported only through explicitly named monetary fields.
    """
    candidates, protected = set(), set()

    def walk(value, currency=None, scale=Decimal(1), comparison=False):
        if isinstance(value, list):
            for item in value:
                walk(item, currency, scale, comparison)
        elif isinstance(value, dict):
            currency = value.get("currency", currency)
            unit = value.get("unit", value.get("units"))
            if unit is not None:
                scale = SCALES.get(unit)
            comparison = comparison or value.get("kind") == "verified_comparison"
            for key, item in value.items():
                if key == "source_text" and isinstance(item, str):
                    try:
                        walk(json.loads(item, parse_float=Decimal), currency, scale)
                    except (ValueError, RecursionError):
                        pass
                elif key in MONEY_FIELDS or (comparison and key in {"earlier", "later", "delta"}):
                    if currency not in CURRENCIES or scale is None or isinstance(item, bool):
                        continue
                    try:
                        amount = Decimal(str(item)) * scale
                        if not amount.is_finite():
                            continue
                    except (InvalidOperation, ValueError):
                        continue
                    target = protected if key in EXACT_FIELDS else candidates
                    target.add((currency, amount))
                    # Declines are often described with an unsigned magnitude.
                    if comparison and key == "delta":
                        target.add((currency, abs(amount)))
                else:
                    walk(item, currency, scale, comparison)

    walk(packets)
    return candidates, protected


def format_financial_speech(draft, packets, question):
    """Return spoken text and auditable mappings, without editing source packets."""
    if EXACT.search(question):
        return draft, []
    candidates, protected = monetary_values(packets)
    bindings = []

    def replace(match):
        currency = CURRENCY_NAMES[(match["before"] or match["after"]).lower()]
        value = Decimal((match["left"] or match["right"]).replace(",", ""))
        if (currency, value) not in candidates or (currency, value) in protected:
            return match[0]
        # A prefix match must not rescale an already scaled amount.
        if re.match(r"\s*(million|thousand|billion|%)\b", draft[match.end() :], re.I):
            return match[0]
        spoken = f"{summary_amount(value)} {CURRENCIES[currency]}"
        bindings.append({"exact_amount": str(value), "currency": currency, "spoken": spoken})
        return spoken

    return MONEY.sub(replace, draft), bindings
