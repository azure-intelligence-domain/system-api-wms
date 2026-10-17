import json

import azure.functions as func
import pytest

import function_app as api
from contracts_test import assert_matches_asyncapi
from fakes import FakeTopic, MemoryStore

RESERVE = {"orderId": "4711", "customerId": "ACME", "orderLines": [{"productId": "SKU-1042", "quantity": 12}]}


def request(method, url, body=None, route=None):
    return func.HttpRequest(method, url, headers={"Correlation-Id": "ord-4711"}, route_params=route or {},
                            body=json.dumps(body).encode() if body is not None else b"")


@pytest.fixture
def wms():
    return MemoryStore(), FakeTopic()


def reserve(wms, body=RESERVE):
    response = api.reserve_stock(request("POST", "/stock-reservations", body), *wms)
    return response, json.loads(response.get_body())


def test_stock_is_reserved_for_an_order(wms):
    response, reserved = reserve(wms)
    assert response.status_code == 201 and reserved["reservationId"] == "RSV-4711" and reserved["warehouseId"] == "BRU-1"
    assert wms[1].types == ["com.acme.supply.stockReserved"]
    assert_matches_asyncapi(wms[1].events[0])
    assert wms[1].events[0]["orderid"] == "4711" and wms[1].events[0]["customerid"] == "ACME"
    stock = json.loads(api.retrieve_stock_item(request("GET", "/stock-items/SKU-1042", route={"productId": "SKU-1042"}), wms[0]).get_body())
    assert stock == {**stock, "onHand": 1000, "reserved": 12, "available": 988}


def test_a_retry_reserves_nothing_more(wms):
    reserve(wms)
    response, _ = reserve(wms)
    assert response.status_code == 200 and len(wms[1].events) == 1
    assert wms[0].get("StockItem", "BRU-1|SKU-1042")["reserved"] == 12


def test_not_enough_stock_reserves_nothing(wms):
    response, problem = reserve(wms, {"orderId": "4712", "orderLines": [{"productId": "SKU-7", "quantity": 1},
                                                                        {"productId": "SKU-1042", "quantity": 1001}]})
    assert response.status_code == 409 and problem["title"] == "Insufficient Stock" and "SKU-1042" in problem["detail"]
    assert not wms[1].events and wms[0].get("StockItem", "BRU-1|SKU-7") is None


def test_lines_of_the_same_product_add_up(wms):
    _, reserved = reserve(wms, {"orderId": "4713", "orderLines": [{"productId": "SKU-1", "quantity": 2}, {"productId": "SKU-1", "quantity": 3}]})
    assert reserved["orderLines"] == [{"productId": "SKU-1", "quantity": 5}]


def test_unknown_reservation_is_404(wms):
    assert api.retrieve_stock_reservation(request("GET", "/x", route={"reservationId": "RSV-1"}), wms[0]).status_code == 404


def test_releasing_gives_the_stock_back(wms):
    reserve(wms)
    req = func.HttpRequest("DELETE", "/stock-reservations/RSV-4711", headers={"Correlation-Id": "ord-4711"},
                           route_params={"reservationId": "RSV-4711"}, params={"reason": "Order cancelled."}, body=b"")
    response = api.release_stock(req, *wms)
    released = json.loads(response.get_body())
    assert response.status_code == 200 and released["lifecycleStatus"] == "released" and released["releaseReason"] == "Order cancelled."
    assert wms[0].get("StockItem", "BRU-1|SKU-1042")["reserved"] == 0
    assert wms[1].types[-1] == "com.acme.supply.stockReleased"
    assert_matches_asyncapi(wms[1].events[-1])
    assert api.release_stock(req, *wms).status_code == 200 and len(wms[1].events) == 2  # a repeat changes nothing
    unknown = func.HttpRequest("DELETE", "/x", headers={"Correlation-Id": "ord-4711"}, route_params={"reservationId": "RSV-1"}, body=b"")
    assert api.release_stock(unknown, *wms).status_code == 404
