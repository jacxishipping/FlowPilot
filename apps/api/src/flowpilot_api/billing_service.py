"""Outbound Stripe Checkout, Customer Portal, and idempotent metered usage reconciliation."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx

from flowpilot_policy import PlanTier, get_plan_limits


class StripeBillingClient:
    """Handles outbound Stripe API calls with clean local simulation when api_key is unset."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "https://api.stripe.com/v1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def create_checkout_session(
        self,
        *,
        organization_id: UUID,
        plan: PlanTier,
        success_url: str,
        cancel_url: str,
    ) -> dict[str, Any]:
        limits = get_plan_limits(plan.value)
        if not self._api_key:
            session_id = f"cs_test_{uuid4().hex[:16]}"
            return {
                "mode": "simulated",
                "checkout_session_id": session_id,
                "url": f"{success_url}?session_id={session_id}&plan={plan.value}",
                "plan": plan.value,
                "monthly_price_usd": limits.monthly_price_usd,
            }

        async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/checkout/sessions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={
                    "mode": "subscription",
                    "success_url": success_url,
                    "cancel_url": cancel_url,
                    "metadata[organization_id]": str(organization_id),
                    "metadata[plan]": plan.value,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return {
            "mode": "live",
            "checkout_session_id": data["id"],
            "url": data["url"],
            "plan": plan.value,
            "monthly_price_usd": limits.monthly_price_usd,
        }

    async def create_portal_session(
        self,
        *,
        stripe_customer_id: str,
        return_url: str,
    ) -> dict[str, Any]:
        if not self._api_key:
            return {
                "mode": "simulated",
                "url": f"{return_url}?portal_customer={stripe_customer_id}",
            }

        async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/billing_portal/sessions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data={"customer": stripe_customer_id, "return_url": return_url},
            )
            resp.raise_for_status()
            data = resp.json()
        return {"mode": "live", "url": data["url"]}

    async def report_meter_event(
        self,
        *,
        event_name: str,
        stripe_customer_id: str,
        value: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not self._api_key:
            return {
                "mode": "simulated",
                "reported": True,
                "event_name": event_name,
                "value": value,
                "idempotency_key": idempotency_key,
            }

        async with httpx.AsyncClient(timeout=20.0, transport=self._transport) as client:
            resp = await client.post(
                f"{self._base_url}/billing/meter_events",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Idempotency-Key": idempotency_key,
                },
                data={
                    "event_name": event_name,
                    "identifier": idempotency_key,
                    "payload[stripe_customer_id]": stripe_customer_id,
                    "payload[value]": str(value),
                },
            )
            resp.raise_for_status()
            return {"mode": "live", "reported": True, "response": resp.json()}
