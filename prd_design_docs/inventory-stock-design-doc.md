# Design Doc: Inventory Stock Level Display
**Status:** Draft  **Author:** Engineering Team  **Date:** 2026-05-28

## Background

Online Boutique has no inventory model. `ProductCatalogService` loads a static YAML product catalog at startup; there is no `stock_count` field anywhere in the system. `CheckoutService.PlaceOrder` charges a payment and ships an order without any stock check. This design introduces an `InventoryService`, extends the `Product` proto, and threads stock awareness through `ProductCatalogService`, `frontend`, and `checkoutservice`.

## Current State

### Product proto (excerpt, unchanged fields)

```protobuf
message Product {
  string         id          = 1;
  string         name        = 2;
  string         description = 3;
  string         picture     = 4;
  Money          price_usd   = 5;
  repeated string categories = 6;
  // fields 7 and 8 are reserved from a prior migration
}
```

No `stock_count` field exists. Field numbers 7 and 8 are reserved.

### PlaceOrder execution order (checkoutservice)

1. `CartService.GetCart`
2. `ProductCatalogService.GetProduct` (per line item)
3. `CurrencyService.Convert` (per item price)
4. `ShippingService.GetQuote`
5. `ShippingService.ShipOrder`
6. `PaymentService.Charge`  ← point of no return
7. `EmailService.SendOrderConfirmation`
8. `CartService.EmptyCart`

`InventoryService.DecrementStock` will be inserted as **step 6b**, immediately after `PaymentService.Charge` (step 6). See Risks for why this ordering is problematic and what v1 accepts.

### Services that call ProductCatalogService today

- `frontend` — `GetProduct`, `ListProducts`, `SearchProducts`
- `checkoutservice` — `GetProduct` (per cart item, during PlaceOrder)
- `recommendationservice` — `ListProducts` (to build recommendation pool)

All three will transitively see the new `stock_count` field on `Product` after the proto change. Callers that do not read field 9 are unaffected (proto3 forward compatibility).

## Proposed Solution

### 1. New service: InventoryService

A new Go microservice backed by Redis. Runs on port **7070**. Not exposed externally.

#### Proto

```protobuf
service InventoryService {
  rpc GetStock(GetStockRequest) returns (GetStockResponse);
  rpc GetStockBatch(GetStockBatchRequest) returns (GetStockBatchResponse);
  rpc DecrementStock(DecrementStockRequest) returns (DecrementStockResponse);
  rpc SetStock(SetStockRequest) returns (SetStockResponse);  // admin/seed only
}

message GetStockRequest  { string product_id = 1; }
message GetStockResponse { int32  stock       = 1; }

message GetStockBatchRequest  { repeated string product_ids = 1; }
message GetStockBatchResponse { map<string, int32> stock    = 1; }

message DecrementStockRequest {
  string product_id = 1;
  int32  quantity   = 2;
}
message DecrementStockResponse {
  bool   success          = 1;
  int32  remaining_stock  = 2;
  string error_message    = 3;  // populated when success=false
}

message SetStockRequest  { string product_id = 1; int32 quantity = 2; }
message SetStockResponse { bool   success     = 1; }
```

#### Redis key structure

```
inventory:{product_id}  →  integer (stock count)
```

`DecrementStock` is implemented using a Lua script for atomicity:

```lua
local current = redis.call('GET', KEYS[1])
if current == false then return {0, 0, 'product not found'} end
if tonumber(current) < tonumber(ARGV[1]) then
  return {0, tonumber(current), 'insufficient stock'}
end
local remaining = redis.call('DECRBY', KEYS[1], ARGV[1])
return {1, remaining, ''}
```

This ensures check-and-decrement is atomic with no race condition between concurrent checkouts.

#### Redis configuration

Redis is deployed with `appendonly yes` (AOF persistence) to survive pod restarts. A `PersistentVolumeClaim` backs the Redis pod. This is a new infrastructure dependency that does not exist today.

### 2. Proto change: Product message

```protobuf
message Product {
  string          id          = 1;
  string          name        = 2;
  string          description = 3;
  string          picture     = 4;
  Money           price_usd   = 5;
  repeated string categories  = 6;
  // fields 7 and 8 are RESERVED
  int32           stock_count = 9;  // NEW: 0 = out of stock; -1 = unknown (InventoryService unavailable)
}
```

Field 9 is chosen because fields 7 and 8 are reserved. Field 9 has never been used. Proto3 default value is `0` — callers that receive a `Product` with field 9 absent will see `stock_count = 0`, which would incorrectly appear as "out of stock". **This is why `InventoryService` must be deployed and populated before `ProductCatalogService` starts populating field 9.** Deployment order is critical (see Deployment Plan).

The value `-1` is used by `ProductCatalogService` as a sentinel when `InventoryService` is unavailable — the frontend treats -1 as "unknown" and renders "In Stock" (optimistic default) rather than "Out of Stock".

### 3. Changes to ProductCatalogService

`ProductCatalogService` adds an `InventoryServiceClient` at startup (new env var `INVENTORY_SERVICE_ADDR`).

**`GetProduct`** — after fetching product from local catalog, call `GetStock(product_id)` and set `product.StockCount`:

```go
func (p *productCatalog) GetProduct(ctx context.Context, req *pb.GetProductRequest) (*pb.Product, error) {
    product, err := p.getProductByID(req.Id)
    if err != nil { return nil, err }

    stockResp, err := p.inventoryClient.GetStock(ctx, &pb.GetStockRequest{ProductId: req.Id})
    if err != nil {
        log.Warnf(ctx, "inventory unavailable for %s: %v", req.Id, err)
        product.StockCount = -1  // sentinel: unknown
    } else {
        product.StockCount = stockResp.Stock
    }
    return product, nil
}
```

**`ListProducts`** — after fetching all products, call `GetStockBatch(all_product_ids)` and populate each product's `StockCount`. One batch call, not N serial calls.

**`SearchProducts`** — same as `ListProducts`: one `GetStockBatch` call after the search is complete.

### 4. Changes to Frontend

**Product detail page (`productHandler`):**
- Read `product.StockCount` from the `GetProduct` response.
- Pass to template as `stock_count`.
- Template logic:

```html
{{if eq .stock_count -1}}
  <!-- sentinel: InventoryService unavailable, render nothing -->
{{else if eq .stock_count 0}}
  <span class="badge out-of-stock">Out of Stock</span>
  <button disabled>Out of Stock</button>
{{else if le .stock_count 10}}
  <span class="badge low-stock">Only {{.stock_count}} left!</span>
  <button>Add to Cart</button>
{{else}}
  <span class="badge in-stock">In Stock</span>
  <button>Add to Cart</button>
{{end}}
```

**Product listing page (`homeHandler`, `searchHandler`):**
- `ListProducts` already returns all products. Each product now has `StockCount`.
- Product card template adds an "Out of Stock" overlay badge when `stock_count == 0`.

### 5. Changes to CheckoutService

A new `InventoryServiceClient` is added. After step 6 (`PaymentService.Charge`), a new step 6b is inserted:

**Step 6b: `InventoryService.DecrementStock`**

For each item in the cart, call `DecrementStock(product_id, quantity)`. If any decrement returns `success=false`:
- Log the failure with the order ID and item details.
- Attempt a `PaymentService.Refund` call (see Open Questions — `Refund` RPC does not exist today).
- Return an error to the caller: `"Item X is no longer available. Your payment has been refunded."`
- Do NOT proceed to step 7 (`EmailService.SendOrderConfirmation`) or step 8 (`CartService.EmptyCart`).

If all decrements succeed, continue with steps 7 and 8 as before.

**Why after Charge, not before?** Decrementing before charge creates a window where stock is held but payment fails, requiring a compensating re-increment. Decrementing after charge means the window where stock goes negative is bounded by concurrent checkout completions for the same SKU — rare for a demo catalog. For a real high-throughput system, a pre-charge reservation system would be required.

## Affected Services

| Service | Change |
|---|---|
| `inventoryservice` | New service — full implementation + Redis deployment |
| `productcatalogservice` | New `InventoryServiceClient`; `GetProduct`, `ListProducts`, `SearchProducts` populate `stock_count`; new env var `INVENTORY_SERVICE_ADDR` |
| `frontend` | Product detail + listing templates updated for stock display; reads `stock_count` from Product |
| `checkoutservice` | New step 6b: `DecrementStock` per cart item after Charge; new `InventoryServiceClient`; new env var `INVENTORY_SERVICE_ADDR` |
| `recommendationservice` | Receives `Product` with `stock_count` field; currently ignores unknown fields — unaffected |
| All other services | No changes |

## Proto Changes

1. `Product` message gains `int32 stock_count = 9` — additive, backward compatible.
2. New file `inventoryservice.proto` defining all `InventoryService` messages and RPCs.
3. `demo.proto` (or `hipstershop.proto`): the `Product` change lives here; all services that import this proto will receive the updated generated code at their next build.

## Deployment Plan

**Order is critical** due to the proto3 zero-value trap (absent field 9 = `stock_count = 0` = appears out of stock):

1. Deploy `inventoryservice` + Redis. Seed initial stock counts via `SetStock` script (`100` units per SKU for all 10 catalog products).
2. Verify `inventoryservice` is healthy and all 10 product IDs return `stock=100` via `GetStock`.
3. Deploy updated `productcatalogservice` (with `InventoryServiceClient` and `stock_count` population). Verify product detail pages in staging show "In Stock" for all products.
4. Deploy updated `checkoutservice` (with `DecrementStock` step 6b). Verify a test checkout decrements stock.
5. Deploy updated `frontend` (stock UI in templates). Verify stock badges render correctly.
6. Rollback: `frontend` and `checkoutservice` rollbacks are backward compatible — they simply stop reading/writing `stock_count`. `productcatalogservice` rollback stops populating field 9 (existing callers see `stock_count=0`, which triggers the `frontend` "Out of Stock" display — **undesirable**). If rolling back `productcatalogservice`, also roll back `frontend` in the same window.

## Risks & Open Questions

**PaymentService has no Refund RPC.** If `DecrementStock` fails after `PaymentService.Charge`, the design requires a `PaymentService.Refund` call that does not currently exist. This is a launch blocker for the checkout enforcement requirement. Either `PaymentService` must be extended with a `Refund` RPC before this feature ships, or the decrement-after-charge approach must be replaced with a pre-charge reservation.

**Proto3 zero-value trap.** If `productcatalogservice` is deployed before `inventoryservice` is seeded, products will have `stock_count=0` (proto3 default for absent int32). `Frontend` will render all products as "Out of Stock". Deployment order and rollback sequencing must be documented in the runbook.

**Redis persistence.** AOF persistence reduces but does not eliminate data loss risk (last-write-before-crash may be lost). A Postgres-backed store would be more durable but significantly more complex for v1. The team must decide before launch.

**Race condition window.** Between two concurrent checkouts for the last unit: both pass `PaymentService.Charge`, then both call `DecrementStock`. The Lua script ensures only one will succeed. The loser must be refunded. At demo scale this is unlikely; at production scale it is a real tail-case.

**`recommendationservice` ignores stock.** Currently `recommendationservice.ListProducts` will continue recommending out-of-stock products. This is a UX gap (recommending unavailable items) that should be addressed in a follow-up.

**checkoutservice latency.** Adding N `DecrementStock` calls (one per cart item) after `PaymentService.Charge` increases the post-charge critical path. These calls should be parallelized (goroutine per item) rather than sequential.
