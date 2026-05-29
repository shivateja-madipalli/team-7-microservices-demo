# PRD: Shipping Estimate on Product Detail Page
**Status:** Draft  **Author:** Product Team  **Date:** 2026-05-28

## Problem Statement

Users on Online Boutique cannot see a shipping cost estimate until they have already committed to the checkout flow. The first time a customer sees what shipping will cost is on the order summary screen, after they have entered their address. At that point, abandoning the cart because shipping is more expensive than expected is disruptive — the user has already invested time in checkout.

Competitor storefronts commonly display an estimated shipping cost on the product detail page ("Ships for $4.99 with standard delivery"). This sets cost expectations before the user adds the item to their cart and reduces late-funnel abandonment driven by shipping sticker shock.

Online Boutique already has a `ShippingService` that computes shipping quotes from a cart. The engineering lift is a targeted addition to the frontend: call `ShippingService.GetQuote` on the product detail page with a synthetic single-item cart and a representative shipping destination, and display the result.

## Goals

- Display a shipping cost estimate on every product detail page.
- The estimate reflects what a user in a representative market would pay for standard delivery of a single unit of that product.
- The estimate is clearly labeled as an estimate ("Estimated shipping: $X.XX") to set accurate expectations.
- No change to the actual checkout flow, cart logic, or final shipping cost calculation.

## Non-Goals

- Personalized shipping estimates based on the user's actual saved address — that requires a login/account system that does not yet exist. The estimate uses a fixed representative address.
- Express or same-day delivery estimate — standard shipping only in this release.
- Shipping estimates on the cart page or checkout page — those already have exact quotes.
- Changes to `ShippingService` itself — only the frontend changes.
- Multi-item or multi-SKU shipping estimates.
- Cached or pre-computed shipping rates stored in a database.

## User Stories

**US-1:** As a shopper viewing a product detail page, I want to see a shipping cost estimate before I add the item to my cart, so that I can evaluate the total cost of buying this item before committing to checkout.

**US-2:** As a price-sensitive shopper, I want the shipping estimate clearly labeled as an estimate so that I understand the exact amount may vary based on my actual address.

**US-3:** As a shopper on a mobile device, I want the shipping estimate displayed near the "Add to Cart" button so that it is visible without scrolling.

## Background: ShippingService Today

`ShippingService` is a Go service running on port 50051 that exposes one relevant RPC:

```
ShippingService.GetQuote(GetQuoteRequest) → GetQuoteResponse
```

`GetQuoteRequest` takes a list of `CartItem` (product ID + quantity) and an `Address`. It returns a `Money` value representing the shipping cost.

Today, `GetQuote` is called only by `checkoutservice` during the `PlaceOrder` flow, after the user has entered their delivery address. `Frontend` does not call `ShippingService` directly — it goes through `checkoutservice` for all shipping interactions.

The new behavior adds a direct call from `frontend` to `ShippingService` on the product detail page render, using a hardcoded representative address.

## Requirements

**R-1 — Estimate call:** The frontend product detail page handler must call `ShippingService.GetQuote` with a single `CartItem` for the displayed product (quantity = 1) and a hardcoded representative `Address` (e.g., New York, NY 10001, US).

**R-2 — Display:** The returned `Money` value must be formatted and displayed on the product detail page near the "Add to Cart" button with the label "Estimated shipping:". If the call fails or times out, the section is omitted silently — shipping estimate is best-effort and must not block page load.

**R-3 — Timeout:** The `GetQuote` call must have a deadline of 300ms. If `ShippingService` does not respond within 300ms, the frontend continues rendering without the estimate.

**R-4 — No changes to ShippingService:** The `GetQuote` proto, the service implementation, and the deployment configuration of `ShippingService` are unchanged.

**R-5 — No changes to checkout:** The actual shipping cost at checkout continues to be calculated by `checkoutservice` using the user's real address. The estimate on the product detail page has no effect on any downstream service or flow.

## Success Metrics

- Cart abandonment rate on the checkout summary page (shipping reveal moment) decreases by ≥10% within 30 days of launch.
- Product detail page p95 load time increases by ≤50ms compared to pre-launch baseline (estimate call is non-blocking).
- `ShippingService` error rate does not increase by more than 2% (new call volume absorbed without degradation).
- Zero incidents of the shipping estimate blocking or slowing product detail page renders.

## Open Questions

1. **Representative address choice:** What address should be used as the hardcoded origin for estimates? US-centric (New York) is the simplest default but may be misleading to non-US users. Should we detect rough geography from the `Accept-Language` header and vary the representative address?
2. **Estimate display when ShippingService is unavailable:** Should the section show "Shipping calculated at checkout" as a fallback, or simply be absent? Showing the fallback text prevents layout shift but requires a content decision.
3. **Currency:** The shipping estimate is returned as `Money` with a currency code. Should the displayed currency match the user's currently selected display currency (requiring a `CurrencyService.Convert` call) or always display in the base currency (USD/EUR)?
