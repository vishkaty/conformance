#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""Validation tests for the UCP SDK Server."""

from absl.testing import absltest
import integration_test_utils
from ucp_sdk.models.schemas.shopping import (
  checkout_update_request as checkout_update_req,
)
from ucp_sdk.models.schemas.shopping import checkout as checkout
from ucp_sdk.models.schemas.shopping import payment_update_request
from ucp_sdk.models.schemas.shopping.payment import (
  Payment,
)
from ucp_sdk.models.schemas.shopping.types import item_update_request
from ucp_sdk.models.schemas.shopping.types import line_item_update_request


# Rebuild models to resolve forward references
checkout.Checkout.model_rebuild(_types_namespace={"Payment": Payment})


class ValidationTest(integration_test_utils.IntegrationTestBase):
  """Tests for input validation and error handling.

  Validated Paths:
  - POST /checkout-sessions
  - PUT /checkout-sessions/{id}
  - POST /checkout-sessions/{id}/complete
  """

  def assert_business_error(
    self,
    response,
    accepted_codes: set[str],
    error_4xx_substring: str,
  ) -> None:
    """Assert a business-level failure in either posture the spec permits.

    The spec models business failures in-band: checkout-rest.md's own
    "Error Response" example answers an all-items-out-of-stock create with
    HTTP 200 and ``ucp.status: "error"`` plus a typed ``messages[]`` entry,
    and partial failures ride as error messages on a created resource. A
    transport-level 4xx rejection (the posture this repo's Flower Shop
    reference implements) is also accepted. Each posture is validated
    strictly:

    - 4xx: the body must describe the error (same substring assertion the
      tests always made against the reference server).
    - 200/201: ``messages[]`` must contain at least one ``type: "error"``
      entry carrying the full message envelope (type, code, content,
      severity — required by message_error.json), at least one such entry
      must use an accepted standardized code (checkout.md error-code table /
      error_code.json examples), and a ``ucp.status: "error"`` response must
      not carry a checkout resource.
    """
    if 400 <= response.status_code < 500:
      self.assertIn(
        error_4xx_substring.lower(),
        response.text.lower(),
        msg=f"Expected '{error_4xx_substring}' in the 4xx error body",
      )
      return

    self.assert_response_status(response, [200, 201])
    data = response.json()
    errors = [m for m in data.get("messages", []) if m.get("type") == "error"]
    self.assertTrue(
      errors,
      "Business failure answered with 2xx must carry an in-band "
      "messages[] entry of type 'error' (checkout.md error handling)",
    )
    for message in errors:
      for field in ("type", "code", "content", "severity"):
        self.assertTrue(
          message.get(field),
          f"Error message missing required field '{field}' "
          f"(message envelope): {message}",
        )
    codes = {m.get("code") for m in errors}
    self.assertTrue(
      codes & accepted_codes,
      f"Expected an error code in {sorted(accepted_codes)}, got "
      f"{sorted(codes)}",
    )
    ucp_envelope = data.get("ucp") or {}
    if ucp_envelope.get("status") == "error":
      self.assertIsNone(
        data.get("id"),
        "A ucp.status='error' response must not include a checkout "
        "resource (checkout.md: 'no resource is included in the response "
        "body')",
      )

  def test_out_of_stock(self) -> None:
    """Test validation for out-of-stock items.

    Given a product with 0 inventory,
    When a checkout creation request is made for this item,
    Then the server either rejects with a 4xx describing the stock problem,
    or answers in-band per the spec's error model with a typed
    'out_of_stock' error message.
    """
    # Get out of stock item from config
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=out_of_stock_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"out_of_stock", "item_unavailable"},
      error_4xx_substring="stock",
    )

  def test_update_inventory_validation(self) -> None:
    """Test that inventory validation is enforced on update.

    Given an existing checkout session with a valid quantity,
    When the line item quantity is updated to exceed available stock,
    Then the server should return a 400 Bad Request error indicating
    insufficient stock.
    """
    response_json = self.create_checkout_session()
    checkout_obj = checkout.Checkout(**response_json)
    checkout_id = checkout_obj.id

    # Update to excessive quantity (e.g. 10000)
    item_update = item_update_request.ItemUpdateRequest(
      id=checkout_obj.line_items[0].item.id,
    )
    line_item_update = line_item_update_request.LineItemUpdateRequest(
      id=checkout_obj.line_items[0].id,
      item=item_update,
      quantity=10001,
    )
    payment_update = payment_update_request.PaymentUpdateRequest(
      instruments=checkout_obj.payment.instruments,
      handlers=[
        h.model_dump(mode="json", exclude_none=True)
        for h in checkout_obj.payment.instruments
      ],
    )

    update_payload = checkout_update_req.CheckoutUpdateRequest(
      id=checkout_id,
      currency=checkout_obj.currency,
      line_items=[line_item_update],
      payment=payment_update,
    )

    response = self.client.put(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}"),
      json=update_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 400)
    self.assertIn(
      "stock", response.text.lower(), msg="Expected 'stock' message"
    )

  def test_product_not_found(self) -> None:
    """Test validation for non-existent products.

    Given a request for a product ID that does not exist in the catalog,
    When a checkout creation request is made,
    Then the server either rejects with a 4xx indicating the product was not
    found, or answers in-band per the spec's error model with a typed error
    message.
    """
    non_existent_item = self.conformance_config.get(
      "non_existent_item",
      {"id": "non_existent_item_1", "title": "Non-existent Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=non_existent_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    self.assert_business_error(
      response,
      accepted_codes={"not_found", "item_unavailable"},
      error_4xx_substring="not found",
    )

  def test_payment_failure(self) -> None:
    """Test handling of payment failures.

    Given a checkout session ready for completion,
    When a payment instrument with a known failing token ('fail_token') is
    submitted,
    Then the server should return a 402 Payment Required error.
    """
    response_json = self.create_checkout_session(handlers=[])
    checkout_id = checkout.Checkout(**response_json).id

    # Use the helper to get valid structure, but request the failing instrument
    # 'instr_fail' is loaded from payment_instruments.csv
    payment_payload = integration_test_utils.get_valid_payment_payload(
      instrument_id="instr_fail"
    )

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 402)

  def test_complete_without_fulfillment(self) -> None:
    """Test completion rejection when fulfillment is missing.

    Given a newly created checkout session without fulfillment details,
    When a completion request is submitted,
    Then the server should return a 400 Bad Request error.
    """
    response_json = self.create_checkout_session(select_fulfillment=False)
    checkout_id = response_json["id"]

    payment_payload = integration_test_utils.get_valid_payment_payload()

    response = self.client.post(
      self.get_shopping_url(f"/checkout-sessions/{checkout_id}/complete"),
      json=payment_payload,
      headers=integration_test_utils.get_headers(),
    )

    self.assert_response_status(response, 400)
    self.assertIn(
      "Fulfillment address and option must be selected",
      response.text,
      msg="Expected error message for missing fulfillment",
    )

  def test_structured_error_messages(self) -> None:
    """Test that error responses carry structured, machine-readable detail.

    Given a request that triggers an error (e.g., out of stock),
    Then a 4xx rejection must carry a structured 'detail' field describing
    the error, and an in-band answer must carry the full message envelope
    (type, code, content, severity) — the structural requirement behind
    both postures.
    """
    # Get out of stock item from config
    out_of_stock_item = self.conformance_config.get(
      "out_of_stock_item",
      {"id": "out_of_stock_item_1", "title": "Out of Stock Item"},
    )

    create_payload = self.create_checkout_payload(
      item_id=out_of_stock_item["id"],
    )

    response = self.client.post(
      self.get_shopping_url("/checkout-sessions"),
      json=create_payload.model_dump(
        mode="json", by_alias=True, exclude_none=True
      ),
      headers=integration_test_utils.get_headers(),
    )

    if 400 <= response.status_code < 500:
      # 4xx posture: the body must be structured, not free text.
      data = response.json()
      self.assertTrue(
        data.get("detail"), "Error response missing 'detail' field"
      )
      self.assertIn("stock", str(data["detail"]).lower())
      return

    # In-band posture: the message envelope IS the structured error; the
    # shared assertion validates every required envelope field.
    self.assert_business_error(
      response,
      accepted_codes={"out_of_stock", "item_unavailable"},
      error_4xx_substring="stock",
    )


if __name__ == "__main__":
  absltest.main()
