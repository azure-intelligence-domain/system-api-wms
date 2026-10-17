"""The WMS, as the WMS System API sees it: stock per warehouse and product, and a reservation per
order (backend-systems/wms/README.md), mapped to the platform's contract.
"""

import kit
from kit import Fact

WAREHOUSE = "BRU-1"
NEW_PRODUCT_STOCK = 1000  # the simulation: a product the WMS has never seen starts with 1,000 units


class InsufficientStock(Exception):
    pass


def stock_item(doc: dict) -> dict:
    return {"_links": {"self": {"href": f"/stock-items/{doc['sku']}"}}, "productId": doc["sku"], "warehouseId": doc["warehouse"],
            "onHand": doc["onHand"], "reserved": doc["reserved"], "available": doc["onHand"] - doc["reserved"]}


def reservation(doc: dict) -> dict:
    out = {"_links": {"self": {"href": f"/stock-reservations/{doc['id']}"}}, "reservationId": doc["id"], "orderId": doc["orderRef"],
           "customerId": doc.get("customer"), "warehouseId": doc["warehouse"],
           "orderLines": [{"productId": line["sku"], "quantity": line["qty"]} for line in doc["lines"]],
           "status": "reserved", "lifecycleStatus": doc["state"].lower(), "reservedAt": doc["createdAt"], "releasedAt": doc.get("releasedAt"),
           "releaseReason": doc.get("releaseReason")}
    return {k: v for k, v in out.items() if v is not None}


def item(store, sku: str) -> dict:
    """The stock of a product, or a new product's starting stock (not saved until it changes)."""
    return store.get("StockItem", f"{WAREHOUSE}|{sku}") or {
        "id": f"{WAREHOUSE}|{sku}", "warehouse": WAREHOUSE, "sku": sku, "onHand": NEW_PRODUCT_STOCK, "reserved": 0}


def reserve(store, request: dict) -> tuple[int, dict, list[Fact]]:
    """Set stock aside for every line of an order, or for none of them."""
    reservation_id = f"RSV-{request['orderId']}"
    existing = store.get("Reservation", reservation_id)
    if existing:  # one reservation per order: a retry changes nothing
        return 200, reservation(existing), []

    wanted: dict[str, int] = {}
    for line in request["orderLines"]:
        wanted[line["productId"]] = wanted.get(line["productId"], 0) + line["quantity"]
    items = {sku: item(store, sku) for sku in wanted}
    short = [sku for sku, qty in wanted.items() if items[sku]["onHand"] - items[sku]["reserved"] < qty]
    if short:
        raise InsufficientStock(", ".join(short))
    for sku, qty in wanted.items():
        doc = {**items[sku], "reserved": items[sku]["reserved"] + qty}
        store.replace("StockItem", doc) if "_etag" in doc else store.create("StockItem", doc)

    doc = {"id": reservation_id, "orderRef": request["orderId"], "customer": request.get("customerId"), "warehouse": WAREHOUSE,
           "lines": [{"sku": sku, "qty": qty} for sku, qty in wanted.items()], "state": "RESERVED", "createdAt": kit.now()}
    store.create("Reservation", {k: v for k, v in doc.items() if v is not None})
    reserved = reservation(doc)
    return 201, reserved, [Fact("StockReserved", {"orderId": request["orderId"], "reservationId": reservation_id,
                                                  "warehouseId": WAREHOUSE, "orderLines": reserved["orderLines"],
                                                  "reservedAt": doc["createdAt"]},
                                order_id=request["orderId"], customer_id=request.get("customerId"))]


def give_back(store, sku: str, qty: int) -> None:
    """Reserved units of a product become available again."""
    while True:
        stock = item(store, sku)
        try:
            store.replace("StockItem", {**stock, "reserved": max(0, stock["reserved"] - qty)})
            return
        except kit.Changed:
            continue  # another reservation changed this product meanwhile


class ReservationNotFound(Exception):
    pass


def release(store, reservation_id: str, reason: str) -> tuple[dict, list[Fact]]:
    """Give a reservation's stock back (soft delete: status released). A repeated release changes nothing."""
    doc = store.get("Reservation", reservation_id)
    if doc is None:
        raise ReservationNotFound
    if doc["state"] == "RELEASED":
        return reservation(doc), []
    doc.update(state="RELEASED", releasedAt=kit.now(), releaseReason=reason)
    store.replace("Reservation", doc)  # first: of two concurrent releases, only one gets here
    for line in doc["lines"]:
        give_back(store, line["sku"], line["qty"])
    released = reservation(doc)
    return released, [Fact("StockReleased", {"orderId": doc["orderRef"], "reservationId": doc["id"], "warehouseId": doc["warehouse"],
                                             "orderLines": released["orderLines"], "reason": reason, "releasedAt": doc["releasedAt"]},
                           order_id=doc["orderRef"], customer_id=doc.get("customer"))]
