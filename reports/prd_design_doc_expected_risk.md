# Expected Risk Levels — PRD + Design Doc Eval Suite
**Author:** Team 7  **Date:** 2026-05-28

This document records the expected risk level for each PRD + Design Doc pair in `prd_design_docs/`. Use it to cross-verify the agent's output against ground truth.

---

## Summary Table

| # | PRD + Design Doc Pair | Expected Risk | Proto Change | New Service | New DB/Infra | Affected Services |
|---|---|---|---|---|---|---|
| 1 | INR Currency Support | **LOW** | No | No | No | currencyservice (data only) |
| 2 | Product Category Filter | **MEDIUM** | Yes (additive field) | No | No | productcatalogservice, frontend |
| 3 | Persistent Order History | **HIGH** | Yes (new field on PlaceOrderRequest) | Yes (orderhistoryservice) | Yes (Postgres) | checkoutservice, frontend, orderhistoryservice |
| 4 | Subscribe & Save | **HIGH** | Yes (new service + checkout fields) | Yes (subscriptionservice + worker) | Yes (Postgres + payment vault) | checkoutservice, frontend, paymentservice, new services |
| 5 | Shipping Estimate on Product Page | **MEDIUM** | No | No | No | frontend (new ShippingService dependency), shippingservice (new caller) |
| 6 | Inventory Stock Level Display | **HIGH** | Yes (Product.stock_count field 9) | Yes (inventoryservice) | Yes (Redis with AOF) | productcatalogservice, checkoutservice, frontend, inventoryservice |

---

## Detailed Reasoning

---

### 1. INR Currency Support
**Files:** `inr-currency-prd.md` / `inr-currency-design-doc.md`
**Expected Risk: LOW**

**Why Low:**
- Single file changed: `src/currencyservice/data/currency_conversion.json` — one new key-value pair added.
- No proto changes. `GetSupportedCurrencies` and `Convert` RPCs are unchanged.
- No source code changes in any service. The loading logic is fully data-driven — `Object.keys(currencyData)` automatically surfaces the new key.
- No new services, no new infrastructure.
- Blast radius: `frontend` and `checkoutservice` are confirmed callers of `currencyservice`. Both handle the new currency transparently (frontend populates dropdown at runtime; checkoutservice passes `user_currency` through without validation).
- Deployment: standard rolling restart of `currencyservice`. Stateless service — zero downtime rollout.
- Primary rollout risk: the ~30–60s restart window where users with INR selected may get `INVALID_ARGUMENT` errors. Recoverable immediately by selecting another currency.

**What the agent should NOT do:**
- Flag `productcatalogservice` as affected — it has a genproto stub for `currencyservice` but no actual runtime call.
- Treat the rolling restart as a high-risk deployment event.

---

### 2. Product Category Filter
**Files:** `filter-prd.md` / `filter-design-doc.md`
**Expected Risk: MEDIUM**

**Why Medium:**
- Proto change: `SearchProductsRequest` and `ListProductsRequest` gain a new `repeated string categories` filter field. This is an additive proto change — existing callers that omit the field get unchanged behavior (empty = no filter applied). Low wire-level risk.
- `ProductCatalogService` implementation changes: filtering logic added to `SearchProducts` and `ListProducts`. The static YAML catalog already has category tags per product; no data migration needed.
- `Frontend` changes: new category filter UI chips on the home and search pages; passes `categories` on calls to `ProductCatalogService`.
- Confirmed callers of `ProductCatalogService` that receive updated proto: `frontend`, `checkoutservice` (reads `GetProduct`, not `ListProducts` or `SearchProducts` — unaffected by filter logic), `recommendationservice` (calls `ListProducts` — receives updated response shape but new field has no behavioral impact on recommendations unless filtering is applied, which it won't be from `recommendationservice`).
- No new services, no new databases.
- Risk factors: proto change touches a high-QPS path (`ListProducts` is called on every homepage render); filtering logic must handle empty/nil `categories` without regressing existing behavior; UI change visible to all users.

**What the agent should NOT call out as high-risk:**
- The proto change alone does not make this high — it is purely additive and backward compatible.
- `recommendationservice` and `checkoutservice` do not need code changes.

---

### 3. Persistent Order History
**Files:** `order-history-prd.md` / `order-history-design-doc.md`
**Expected Risk: HIGH**

**Why High:**
- New service: `orderhistoryservice` — full implementation, deployment, monitoring, and runbooks required.
- New database: PostgreSQL instance. New operational dependency (backup, restore, failover, schema migrations).
- Proto changes: `PlaceOrderRequest` gains `string account_id = 7` (new field on the most critical proto in the system — the checkout RPC). Field 4 is already reserved; field 7 must not conflict.
- `checkoutservice` changes: new step 7b (`SaveOrder`) inserted into `PlaceOrder` — the highest-consequence flow in the system. An error in this step must be handled without failing the overall checkout (best-effort write).
- Hard external dependency: the auth system does not exist. `account_id` cannot be populated without it. The feature is inert until auth ships.
- GDPR/data retention risk: order records must support deletion for EU users. `DeleteOrderHistory` RPC is required before EU launch.
- Blast radius: `checkoutservice` (new call in critical path), `frontend` (new page, new client, nav change), `orderhistoryservice` (net-new).

**Key risks the agent should surface:**
- Best-effort write: if `SaveOrder` fails, payment has completed and the user has no history record — same failure mode as `EmailService` (§8.3, §10.8 of Product Handbook).
- Non-atomic checkout: `PlaceOrder` is not transactional. Crash between step 7 and step 7b leaves a paid, unrecorded order.
- Auth dependency is a launch blocker.
- Postgres operational readiness: team needs runbooks before production.

---

### 4. Subscribe & Save
**Files:** `subscribe-and-save-prd.md` / `subscribe-and-save-design-doc.md`
**Expected Risk: HIGH**

**Why High (the most complex scenario in the suite):**
- Multiple new services: subscription manager, billing worker, subscription frontend component.
- Payment vault: recurring billing requires storing tokenized payment credentials. This introduces a PCI-DSS scope concern — a compliance risk dimension absent from all other scenarios.
- New proto definitions for the subscription service + changes to `PlaceOrderRequest` to carry subscription opt-in.
- `PaymentService` changes: new `ChargeSaved` RPC for recurring charges using vaulted credentials.
- `CheckoutService` changes: conditional subscription opt-in path branching from the normal checkout flow.
- Worker/cron: a background job runs on schedule to trigger renewals — new infrastructure pattern (not present anywhere else in the system today).
- FTC click-to-cancel compliance: cancellation flow must be validated before launch (legal/regulatory risk).
- Failed payment recovery: retry logic + dunning emails. Multi-step failure handling across `PaymentService` and `EmailService`.
- Discount application: per-category discount percentages must be applied consistently across the subscription renewal path without duplicating pricing logic.

**Key risks the agent should surface:**
- PCI-DSS scope expansion (payment credential storage).
- Non-atomic subscription creation at checkout (payment completes, subscription record write fails).
- FTC compliance for cancellation flow.
- Worker failure modes: idempotency of renewal attempts, duplicate charge prevention.

---

### 5. Shipping Estimate on Product Page
**Files:** `shipping-estimate-prd.md` / `shipping-estimate-design-doc.md`
**Expected Risk: MEDIUM**

**Why Medium:**
- No proto changes. `ShippingService.GetQuote` is unchanged; the existing proto is reused.
- No new service. No new database or infrastructure.
- The change is entirely in `frontend`: new gRPC client connection to `ShippingService`, one new helper function, template change, one manifest change (`SHIPPING_SERVICE_ADDR`).
- `ShippingService` itself has no code changes — it is an existing, confirmed, stable service.
- However, this introduces a **new inter-service dependency**: `frontend → shippingservice`. This is a meaningful architectural change — today `frontend` never calls `shippingservice` directly. If `shippingservice` becomes unavailable, this new call path is affected (mitigated by the 300ms timeout + graceful degradation).
- Increased load on `shippingservice`: product detail page traffic >> checkout traffic. `shippingservice` is sized for checkout QPS; it may need capacity review before rollout.
- Manifest dependency ordering risk: if the binary ships before `SHIPPING_SERVICE_ADDR` is in the manifest, `frontend` will crash at startup.

**What pushes this above LOW:**
- New inter-service dependency changes the system topology.
- `shippingservice` load increase is proportional to product page traffic (potentially 10x+ vs. current checkout-only load).
- Manifest ordering error is a real blast-radius risk (frontend crash = site down).

**What keeps this below HIGH:**
- No proto changes, no data stores, no new services.
- Graceful degradation: estimate is best-effort; page renders fine without it.
- The change is contained to `frontend` and a pre-existing service.

---

### 6. Inventory Stock Level Display
**Files:** `inventory-stock-prd.md` / `inventory-stock-design-doc.md`
**Expected Risk: HIGH**

**Why High:**
- New service: `inventoryservice` — full implementation, Redis deployment, AOF persistence configuration, and a seed script.
- New infrastructure: Redis with persistent volume. Operational runbooks (backup, failover) required.
- Proto change: `Product.stock_count` at field 9. This is a cross-cutting change — every service that imports the `Product` proto receives the updated generated code: `frontend`, `checkoutservice`, `recommendationservice`, `productcatalogservice`. While the change is additive (backward compatible on the wire), the **proto3 zero-value trap** is a real deployment risk: if `productcatalogservice` ships before `inventoryservice` is seeded, all products appear "Out of Stock" (`stock_count = 0`).
- `checkoutservice` changes: new step 6b (`DecrementStock`) inserted after `PaymentService.Charge` — the point of no return. If decrement fails post-charge, a refund is required, but `PaymentService.Refund` does not currently exist. This is a **launch blocker**.
- `productcatalogservice` changes: new `InventoryServiceClient`; `GetProduct`, `ListProducts`, and `SearchProducts` all fan out to `InventoryService`. This adds a new failure point to three high-QPS paths.
- `frontend` changes: template logic across product detail and listing pages.
- Deployment order matters critically — incorrect order causes a site-wide "Out of Stock" incident.

**Key risks the agent should surface:**
- Proto3 zero-value trap: `stock_count = 0` (absent field) renders as "Out of Stock" — deployment sequence must be exact.
- `PaymentService.Refund` does not exist: launch blocker for checkout enforcement.
- `productcatalogservice` now has a synchronous dependency on `inventoryservice` in all product-fetch paths — `inventoryservice` availability directly affects product page availability.
- Race condition: two concurrent checkouts for the last unit — Lua atomicity mitigates, but the loser requires a refund flow that doesn't exist.
- `recommendationservice` will continue surfacing out-of-stock products (out of scope for v1 but a known UX gap).

---

## Scoring Guide for Agent Verification

When running the agent against each scenario, compare its output to this table:

| Dimension | INR | Filter | Order History | Subscribe & Save | Shipping Estimate | Inventory |
|---|---|---|---|---|---|---|
| Correct risk level | LOW | MEDIUM | HIGH | HIGH | MEDIUM | HIGH |
| Proto change detected | — | Yes | Yes | Yes | — | Yes |
| New service identified | — | — | orderhistoryservice | subscription+worker | — | inventoryservice |
| New DB/infra identified | — | — | Postgres | Postgres+vault | — | Redis |
| Correct blast radius | frontend, checkoutservice | frontend, productcatalogservice | checkoutservice, frontend, orderhistoryservice | checkoutservice, frontend, paymentservice | frontend, shippingservice | productcatalogservice, checkoutservice, frontend |
| Key risk surfaced | restart window | additive proto, filtering regression | best-effort write, auth blocker | PCI-DSS, FTC compliance, idempotency | load increase on shippingservice, manifest ordering | zero-value trap, missing Refund RPC |
| False positive services | productcatalogservice must be excluded | recommendationservice, checkoutservice minimal impact | — | — | — | recommendationservice (passive, no code change) |
