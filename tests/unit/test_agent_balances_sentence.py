"""`_balances_sentence` (pure): the code-rendered answer to a pure balance question, built
directly from `ReadTools.get_balances()`'s own result shape. No network, no LLM.
"""

from splitbot.agent.agent import _balances_sentence


def _transfer(from_id, from_name, to_id, to_name, amount):
    return {"from": from_id, "from_name": from_name, "to": to_id, "to_name": to_name,
            "amount": amount, "amount_minor": 0}


def _members(*names):
    return [{"user_id": i, "name": n, "net": "0", "net_minor": 0} for i, n in enumerate(names, start=1)]


def test_one_transfer_is_rendered_as_one_exact_line():
    result = {"balances": {"ILS": {"members": _members("זיו", "דני"),
                                   "transfers": [_transfer(2, "דני", 1, "זיו", "20")]}}}
    assert _balances_sentence(result) == "💰 מצב החובות\n\nמ־דני\nאל: זיו\nסכום: ₪20"


def test_several_currencies_each_get_their_own_symbol_in_the_results_own_order():
    result = {"balances": {
        "ILS": {"members": _members("זיו", "דני"), "transfers": [_transfer(2, "דני", 1, "זיו", "20")]},
        "USD": {"members": _members("זיו", "משה"), "transfers": [_transfer(3, "משה", 1, "זיו", "15.99")]},
        "EUR": {"members": _members("זיו", "מיכל"), "transfers": [_transfer(4, "מיכל", 1, "זיו", "10")]},
        "XXX": {"members": _members("זיו", "מיכל"), "transfers": [_transfer(4, "מיכל", 1, "זיו", "5")]},
    }}
    assert _balances_sentence(result) == (
        "💰 מצב החובות\n\n"
        "מ־דני\nאל: זיו\nסכום: ₪20\n\n"
        "מ־משה\nאל: זיו\nסכום: $15.99\n\n"
        "מ־מיכל\nאל: זיו\nסכום: €10\n\n"
        "מ־מיכל\nאל: זיו\nסכום: XXX5"
    )


def test_no_transfers_in_any_currency_is_the_fixed_no_debts_sentence():
    result = {"balances": {"ILS": {"members": _members("זיו", "דני"), "transfers": []}}}
    assert _balances_sentence(result) == "אין חובות פתוחים כרגע."

    # also true with no currencies at all (an empty ledger)
    assert _balances_sentence({"balances": {}}) == "אין חובות פתוחים כרגע."


def test_multiple_transfers_in_one_currency_all_appear_joined_by_newline_in_order():
    result = {"balances": {"ILS": {
        "members": _members("זיו", "דני", "משה", "מיכל"),
        "transfers": [
            _transfer(4, "מיכל", 3, "משה", "140"),
            _transfer(1, "זיו", 2, "דני", "20"),
            _transfer(4, "מיכל", 2, "דני", "20"),
        ],
    }}}
    assert _balances_sentence(result) == (
        "💰 מצב החובות\n\n"
        "מ־מיכל\nאל: משה\nסכום: ₪140\n\n"
        "מ־זיו\nאל: דני\nסכום: ₪20\n\n"
        "מ־מיכל\nאל: דני\nסכום: ₪20"
    )
