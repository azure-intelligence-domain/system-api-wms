"""[Supply] WMS System API (openapi.yaml): stock and stock reservations in the warehouse, in the
platform's contract. Only the people gateway reaches it (Easy Auth), and only building blocks on
the platform's allow-list reach the gateway's system-apis product (ADR 0014). Every reservation is
a supply fact on evgt-intplat-supply (asyncapi.yaml).
"""

import azure.functions as func

import kit
import wms

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)  # Easy Auth guards the door


@kit.refusals
def reserve_stock(req: func.HttpRequest, store, topic) -> func.HttpResponse:
    journey = kit.journey(req)
    for _ in range(3):
        try:
            status, reserved, facts = wms.reserve(store, kit.body_of(req, "reservationRequest"))
            break
        except wms.InsufficientStock as short:
            raise kit.refused(409, "Insufficient Stock", f"Not enough stock of {short}; nothing was reserved.",
                              "insufficient-stock", req) from None
        except kit.Changed:
            continue  # another reservation took stock meanwhile: count again
    else:
        raise RuntimeError("Stock kept changing; gave up after 3 attempts.")
    topic.publish(facts, journey)
    return kit.hal(status, reserved, {"Location": reserved["_links"]["self"]["href"]})


@kit.refusals
def retrieve_stock_reservation(req: func.HttpRequest, store) -> func.HttpResponse:
    kit.journey(req)
    doc = store.get("Reservation", req.route_params["reservationId"])
    if doc is None:
        raise kit.refused(404, "Reservation Not Found", "The WMS has no reservation with this id.", "not-found", req)
    return kit.hal(200, wms.reservation(doc))


@kit.refusals
def retrieve_stock_item(req: func.HttpRequest, store) -> func.HttpResponse:
    kit.journey(req)
    return kit.hal(200, wms.stock_item(wms.item(store, req.route_params["productId"])))


# --- Routes (host.json: no route prefix, so the paths are the contract's) -----------------------


@app.route(route="stock-reservations", methods=["POST"])
def reserveStock(req: func.HttpRequest) -> func.HttpResponse:
    return reserve_stock(req, kit.system_store(), kit.domain_topic())


@app.route(route="stock-reservations/{reservationId}", methods=["GET"])
def retrieveStockReservation(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_stock_reservation(req, kit.system_store())


@app.route(route="stock-items/{productId}", methods=["GET"])
def retrieveStockItem(req: func.HttpRequest) -> func.HttpResponse:
    return retrieve_stock_item(req, kit.system_store())
