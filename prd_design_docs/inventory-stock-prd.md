# PRD: Inventory Stock Level Display
**Status:** Draft  **Author:** Product Team  **Date:** 2026-05-28

## Problem Statement

Online Boutique currently has no concept of inventory. Every product always appears available with no indication of how many units are in stock. This is appropriate for a demo, but creates a poor experience if the platform is used for real commerce: users cannot distinguish between abundant and scarce items, there is no urgency signal to encourage timely purchase, and the system will happily let a user "buy" an item even if it is theoretically out of stock.

Displaying stock levels — particularly low-stock warnings ("Only 3 left!") — is a standard e-commerce conversion pattern. It creates urgency, sets accurate expectations, and allows the platform to surface scarcity as a signal. Internally, having an authoritative inventory count is a prerequisite for the checkout flow to enforce purchase limits and prevent overselling.

## Goals

- Every product in the catalog has an associated stock count, stored durably.
- Product detail pages display a stock indicator: "In Stock", "Only N left!" (when N ≤ 10), or "Out of Stock".
- Product listing pages (home, search results) show an "Out of Stock" badge on unavailable items.
- The checkout flow prevents purchase of out-of-stock items.
- Inventory counts are decremented atomically at the time of a successful charge (not at cart add, to avoid ghost reservations).

## Non-Goals

- Inventory reservations during the checkout flow (soft locks) — decrement happens on confirmed purchase only.
- Inventory replenishment or supplier integration — stock counts are set manually or via an admin API; automatic restocking is out of scope.
- Real-time stock push notifications or "back in stock" alerts — out of scope for this release.
- Per-warehouse or regional inventory — a single global stock count per SKU.
- Admin UI for managing inventory counts — counts are seeded via a script and updated via the `InventoryService` API directly.

## User Stories

**US-1:** As a shopper on a product detail page, I want to see how many units are left in stock so that I can make a timely decision about whether to buy before it sells out.

**US-2:** As a shopper browsing product listings, I want out-of-stock items clearly marked so that I do not click into a product only to find it unavailable.

**US-3:** As a shopper attempting to check out with an item that just sold out, I want a clear error message at checkout (not a silent failure) so that I can adjust my cart.

**US-4:** As a shopper, I want the "Add to Cart" button on a product detail page to be disabled and labeled "Out of Stock" when the item has zero units remaining.

## Background: How ProductCatalogService Works Today

`ProductCatalogService` (Go, port 3550) loads its product catalog from a static YAML file at startup (`products.pb.json`). It exposes:
- `ListProducts` — returns all products
- `GetProduct(id)` — returns a single product by ID
- `SearchProducts(query)` — text-match search over name + description

The `Product` proto message today has: `id`, `name`, `description`, `picture`, `price_usd`, and `categories`. There is no `stock_count` field. Adding inventory requires either extending this proto or introducing a separate `InventoryService`.

This PRD requires a new `InventoryService` to keep inventory concerns separate from catalog concerns, and a proto change to `ProductCatalogService` to optionally carry a stock level in its response (for efficient bundling on list and detail pages).

## Requirements

**R-1 — Inventory Service:** A new `InventoryService` backed by a Redis instance stores `product_id → stock_count` mappings. It exposes: `GetStock(product_id)`, `GetStockBatch(product_ids)`, `DecrementStock(product_id, quantity)` (atomic, fails if insufficient stock), and `SetStock(product_id, quantity)` (admin/seed use only).

**R-2 — Product proto extension:** The `Product` message gains an optional `int32 stock_count = 9` field. `ProductCatalogService.GetProduct` and `ListProducts` populate this field by calling `InventoryService.GetStock(Batch)` before returning. This allows `frontend` to get product + stock in one logical call.

**R-3 — Frontend: Product detail page:** Display stock indicator based on `product.stock_count`. Disable "Add to Cart" button and show "Out of Stock" label when `stock_count == 0`.

**R-4 — Frontend: Product listing page:** Show "Out of Stock" badge on product cards where `stock_count == 0`.

**R-5 — Checkout enforcement:** `CheckoutService.PlaceOrder` must call `InventoryService.DecrementStock` atomically after `PaymentService.Charge` succeeds. If decrement fails (insufficient stock), `PlaceOrder` returns an error to the user. (Note: payment has already been charged at this point — a compensating refund must be issued. See Risks.)

**R-6 — Stock seeding:** A one-time script seeds initial stock counts for all products via `SetStock`. Initial counts are configurable; the default is 100 units per SKU.

## Success Metrics

- Product detail page renders with an accurate stock indicator for 100% of products within one sprint of launch.
- Zero oversell incidents (checkout completing for an out-of-stock item) in the first 90 days.
- Checkout conversion rate on low-stock items (≤10 units) is ≥5% higher than the pre-launch baseline for the same items (urgency signal working).
- `ProductCatalogService` p95 latency increases by ≤30ms after the `InventoryService` call is added (batch call, not serial per-product).

## Open Questions

1. **Decrement timing:** Decrementing after `PaymentService.Charge` is the safest point for preventing ghost reservations, but creates a window where a user pays for an item that another user has just bought. Should we add a pre-charge soft-lock (reserve stock for 5 minutes at cart add) in v1, accepting the added complexity?
2. **Refund on decrement failure:** If `DecrementStock` fails after payment, `checkoutservice` must issue a `PaymentService.Refund` call. `PaymentService` is currently a mock with no refund RPC. Does this feature require adding `Refund` to the payment service before launch?
3. **Redis persistence:** Redis with the default configuration is ephemeral. Stock counts are lost on Redis restart. Should this use Redis with AOF persistence, or a Postgres-backed inventory store instead?
4. **Stock seeding for existing catalog:** The 10 demo products in the current catalog each need an initial stock count. Is 100 units per SKU the right default for a demo environment, or do we want some products seeded at low counts (≤5) to exercise the urgency display in demos?
