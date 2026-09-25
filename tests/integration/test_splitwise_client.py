import httpx
import pytest
import respx

from splitbot.splitwise.base import BackendError, NewExpense, Share
from splitbot.splitwise.client import BASE_URL, SplitwiseClient, agorot_to_str, str_to_agorot

EXPENSE = NewExpense(
    group_id=7,
    description="test",
    total=1001,
    shares=[Share(user_id=1, paid=1001, owed=501), Share(user_id=2, paid=0, owed=500)],
)


@pytest.fixture
def client():
    return SplitwiseClient("key")


def test_agorot_are_formatted_without_floats():
    assert agorot_to_str(1001) == "10.01"
    assert agorot_to_str(5) == "0.05"
    assert str_to_agorot("10.0") == 1000


@respx.mock
def test_http_200_with_errors_is_a_failure(client):
    respx.post(BASE_URL + "create_expense").mock(
        return_value=httpx.Response(200, json={"expenses": [], "errors": {"base": ["nope"]}})
    )
    with pytest.raises(BackendError):
        client.add_expense(EXPENSE)


@respx.mock
def test_bearer_key_and_explicit_ils_are_sent(client):
    route = respx.post(BASE_URL + "create_expense").mock(
        return_value=httpx.Response(
            200,
            json={
                "errors": {},
                "expenses": [
                    {
                        "id": 99,
                        "group_id": 7,
                        "description": "test",
                        "cost": "10.01",
                        "currency_code": "ILS",
                        "details": "",
                        "deleted_at": None,
                        "users": [
                            {"user_id": 1, "paid_share": "10.01", "owed_share": "5.01"},
                            {"user_id": 2, "paid_share": "0.0", "owed_share": "5.00"},
                        ],
                    }
                ],
            },
        )
    )
    created = client.add_expense(EXPENSE)
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer key"
    assert b'"currency_code":"ILS"' in request.content.replace(b" ", b"")
    assert created.id == 99 and created.total == 1001


@respx.mock
def test_timeout_becomes_backend_error(client):
    respx.get(BASE_URL + "get_current_user").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BackendError):
        client.get_current_user()


@respx.mock
def test_delete_with_success_false_is_a_failure(client):
    respx.post(BASE_URL + "delete_expense/99").mock(
        return_value=httpx.Response(200, json={"success": False, "errors": {}})
    )
    with pytest.raises(BackendError):
        client.delete_expense(99)


@respx.mock
def test_delete_without_success_flag_is_not_trusted(client):
    respx.post(BASE_URL + "delete_expense/99").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(BackendError):
        client.delete_expense(99)


@respx.mock
def test_delete_with_success_true_passes(client):
    respx.post(BASE_URL + "delete_expense/99").mock(
        return_value=httpx.Response(200, json={"success": True, "errors": {}})
    )
    client.delete_expense(99)


@respx.mock
def test_soft_deleted_expense_is_readable_and_marked_deleted(client):
    respx.get(BASE_URL + "get_expense/99").mock(
        return_value=httpx.Response(
            200,
            json={
                "expense": {
                    "id": 99,
                    "group_id": 7,
                    "description": "test",
                    "cost": "10.01",
                    "currency_code": "ILS",
                    "details": None,
                    "deleted_at": "2026-09-25T10:00:00Z",
                    "users": [],
                }
            },
        )
    )
    expense = client.get_expense(99)
    assert expense.deleted is True
    assert expense.details == ""


def test_shares_that_do_not_sum_to_total_are_rejected_before_sending(client):
    bad = EXPENSE.model_copy(update={"total": 2000})
    with pytest.raises(BackendError):
        client.add_expense(bad)
