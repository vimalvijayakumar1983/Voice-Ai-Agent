import copy

import pytest

from app.livekit_runtime.financial_speech import format_financial_speech


def packet(field, value, **updates):
    return [{"data": {"currency": "AED", "units": "units", field: value, **updates}}]


@pytest.mark.parametrize(
    "value,spoken",
    [
        ("8236000", "8.24 million"),
        ("8260000", "8.26 million"),
        ("426000", "426 thousand"),
        ("523000", "523 thousand"),
        ("634769.40", "634.77 thousand"),
        ("450", "450"),
        ("-8236000", "-8.24 million"),
        ("1000000", "1.00 million"),
        ("999999", "1.00 million"),
        ("8235000", "8.24 million"),
    ],
)
def test_money_summaries_are_decimal_rounded_not_rescaled_guesses(value, spoken):
    packets = packet("revenue_ex_vat", value)
    saved = copy.deepcopy(packets)
    text, bindings = format_financial_speech(f"Sales were AED {value}.", packets, "Sales?")
    assert text == f"Sales were {spoken} dirhams."
    assert bindings[0]["exact_amount"] == value
    assert packets == saved


@pytest.mark.parametrize(
    "question",
    [
        "Exact sales?",
        "Invoice amount?",
        "Payment due?",
        "Collect the full amount",
        "Reconcile the account",
    ],
)
def test_exact_requests_never_round(question):
    draft = "AED 8236000.51"
    assert format_financial_speech(draft, packet("revenue_ex_vat", "8236000.51"), question) == (
        draft,
        [],
    )


@pytest.mark.parametrize("field", ["amount_due", "invoice_amount", "payment_amount"])
def test_action_amounts_remain_exact_even_when_question_is_generic(field):
    draft = "AED 8236000.51"
    assert format_financial_speech(draft, packet(field, "8236000.51"), "What is it?") == (draft, [])


@pytest.mark.parametrize("field", ["purchase_amount", "receivable_amount", "outstanding_amount"])
def test_reusable_summary_fields(field):
    assert format_financial_speech("AED 426000", packet(field, "426000"), "Summary?")[0] == (
        "426 thousand dirhams"
    )


def test_wrong_currency_unknown_unit_and_nonmonetary_fields_are_not_rewritten():
    for packets in [
        packet("revenue_ex_vat", "8236000", currency="USD"),
        packet("revenue_ex_vat", "8236000", units="unknown"),
        packet("quantity", "8236000"),
    ]:
        assert format_financial_speech("AED 8236000", packets, "Summary?") == ("AED 8236000", [])
    original = "Invoice ID 8236000, phone 8236000, 2026, quantity 426000, 2.88%."
    assert format_financial_speech(original, packet("revenue_ex_vat", "8236000"), "Summary?") == (
        original,
        [],
    )


def test_prefix_suffix_currency_and_explicit_source_scale():
    packets = packet("purchase_amount", "426", units="thousands", currency="USD")
    assert format_financial_speech("426000 dollars", packets, "Summary?")[0] == (
        "426 thousand US dollars"
    )
    assert format_financial_speech("USD 426000", packets, "Summary?")[0] == (
        "426 thousand US dollars"
    )
    assert (
        format_financial_speech(
            "USD 426", packet("purchase_amount", "426", currency="USD"), "Summary?"
        )[0]
        == "426 US dollars"
    )


def test_already_scaled_amount_is_not_scaled_again():
    for draft in ["AED 8.24 million", "8.24 million dirhams"]:
        assert format_financial_speech(draft, packet("revenue_ex_vat", "8.24"), "Summary?") == (
            draft,
            [],
        )
