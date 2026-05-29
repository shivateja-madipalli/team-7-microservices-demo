# Design Doc: Shipping Estimate on Product Detail Page
**Status:** Draft  **Author:** Engineering Team  **Date:** 2026-05-28

## Background

`ShippingService` already computes shipping quotes via `GetQuote(GetQuoteRequest)`. Today the only caller is `checkoutservice`, which invokes it during `PlaceOrder` after the user has entered a real delivery address. `Frontend` has no direct connection to `ShippingService`.

This design adds a new direct gRPC connection from `frontend` to `ShippingService` so the product detail page handler can fetch and display a shipping estimate. The proto definitions, `ShippingService` implementation, and checkout flow are untouched.

## Current State

### Who calls ShippingService today

```
checkoutservice → ShippingService.GetQuote  (during PlaceOrder, step 4)
checkoutservice → ShippingService.ShipOrder (during PlaceOrder, step 5)
```

No other service calls `ShippingService`. `Frontend` reaches `ShippingService` only indirectly, through `checkoutservice.PlaceOrder`.

### Frontend service clients (existing)

`frontend` today maintains gRPC client connections to:
- `productcatalogservice` (product listings, detail)
- `currencyservice` (price display conversion)
- `cartservice` (add/remove/get cart)
- `checkoutservice` (place order)
- `recommendationservice` (homepage recommendations)
- `adservice` (banner ads)

`shippingservice` is not in this list. This design adds it.

### Proto definitions (unchanged by this design)

```protobuf
service ShippingService {
  rpc GetQuote(GetQuoteRequest) returns (GetQuoteResponse);
  rpc ShipOrder(ShipOrderRequest) returns (ShipOrderResponse);
}

message GetQuoteRequest {
  Address          address = 1;
  repeated CartItem items  = 2;
}

message GetQuoteResponse {
  Money cost_usd = 1;
}

message CartItem {
  string product_id = 1;
  int32  quantity   = 2;
}
```

`GetQuoteResponse.cost_usd` is typed `Money` with an embedded currency code. Despite the field name `cost_usd`, the service returns whatever currency the caller passes as context — in practice it returns USD because that is what `checkoutservice` uses. This is unchanged.

## Proposed Changes

### 1. New ShippingService client in frontend

`frontend/main.go` initializes all gRPC client connections at startup. A new connection is added:

```go
// Existing pattern (example from currencyservice):
mustConnGRPC(ctx, &svc.currencySvcConn, os.Getenv("CURRENCY_SERVICE_ADDR"))

// New addition:
mustConnGRPC(ctx, &svc.shippingSvcConn, os.Getenv("SHIPPING_SERVICE_ADDR"))
```

`SHIPPING_SERVICE_ADDR` is already set as a Kubernetes environment variable on all other services that call `shippingservice`. It must be added to the `frontend` Deployment manifest.

```go
// In frontendServer struct:
shippingSvcConn *grpc.ClientConn
```

### 2. New helper: getShippingEstimate

```go
// Representative address used for all estimates.
// Chosen as a common US metropolitan ZIP; not personalized.
var estimateAddress = &pb.Address{
    StreetAddress: "1600 Amphitheatre Pkwy",
    City:          "Mountain View",
    State:         "CA",
    Country:       "USA",
    ZipCode:       94043,
}

func (fe *frontendServer) getShippingEstimate(ctx context.Context, productID string) (*pb.Money, error) {
    ctx, cancel := context.WithTimeout(ctx, 300*time.Millisecond)
    defer cancel()

    r, err := pb.NewShippingServiceClient(fe.shippingSvcConn).GetQuote(ctx, &pb.GetQuoteRequest{
        Address: estimateAddress,
        Items: []*pb.CartItem{
            {ProductId: productID, Quantity: 1},
        },
    })
    if err != nil {
        return nil, err
    }
    return r.GetCostUsd(), nil
}
```

### 3. Product detail page handler change

`frontend/handlers.go` contains `(fe *frontendServer) productHandler`. After fetching the product from `ProductCatalogService`, a best-effort call to `getShippingEstimate` is added:

```go
func (fe *frontendServer) productHandler(w http.ResponseWriter, r *http.Request) {
    // ... existing: get product, get recommendations, get currencies, convert price ...

    shippingEst, err := fe.getShippingEstimate(r.Context(), p.GetId())
    if err != nil {
        // Non-fatal: log and continue. Estimate is omitted from the template.
        log.Warnf(r.Context(), "shipping estimate unavailable for product %s: %v", p.GetId(), err)
        shippingEst = nil
    }

    if err := templates.ExecuteTemplate(w, "product", map[string]interface{}{
        // ... existing template vars ...
        "shipping_estimate": shippingEst,  // nil-safe in template
    }); err != nil {
        // ...
    }
}
```

### 4. Template change

`frontend/templates/product.html` — add one line near the "Add to Cart" button:

```html
{{if .shipping_estimate}}
<p class="shipping-estimate">
  Estimated shipping: {{renderMoney .shipping_estimate}}
</p>
{{end}}
```

`renderMoney` is an existing template helper already used for product prices.

### 5. Kubernetes manifest change

`kubernetes-manifests/frontend.yaml` — add `SHIPPING_SERVICE_ADDR` to the frontend container's env block:

```yaml
- name: SHIPPING_SERVICE_ADDR
  value: "shippingservice:50051"
```

This is the same value used in the `checkoutservice` manifest.

## Affected Services

| Service | Change |
|---|---|
| `frontend` | New `ShippingServiceClient`; new `getShippingEstimate` helper; `productHandler` calls estimate; product template updated; Deployment manifest updated with `SHIPPING_SERVICE_ADDR` |
| `shippingservice` | No code changes. Receives additional `GetQuote` calls from `frontend` (one per product detail page render). |
| `checkoutservice` | No changes |
| All other services | No changes |

## Proto Changes

None. `shippingservice.proto`, `frontend`-relevant protos, and all other proto files are unchanged.

## Deployment Plan

1. Update `frontend` Deployment manifest in `kubernetes-manifests/frontend.yaml` to add `SHIPPING_SERVICE_ADDR`.
2. Build and deploy updated `frontend` image.
3. Verify on staging: product detail page shows "Estimated shipping: $X.XX" for several products.
4. Verify graceful degradation: if `shippingservice` is unavailable (scale to 0 replicas briefly in staging), product detail page still renders without error; estimate section is absent.
5. Monitor `shippingservice` RPS and latency after production rollout. The new call volume from `frontend` (one `GetQuote` per product page render) will increase `shippingservice` RPS proportionally to product page traffic.

## Risks & Open Questions

**Increased ShippingService load.** `ShippingService.GetQuote` will now be called on every product detail page render, not just during checkout. Product page traffic is significantly higher than checkout traffic. Load testing `shippingservice` with realistic product page QPS before rollout is required.

**Hardcoded representative address.** The estimate uses a fixed US address. A user in Germany will see a US-domestic shipping estimate that does not reflect their actual cost. This is a known limitation acceptable for v1 (the PRD documents it as out of scope to personalize).

**Currency mismatch.** `GetQuoteResponse.cost_usd` returns a `Money` value that in practice contains USD. If the user has selected a non-USD display currency, the estimate will display in USD while product prices display in their selected currency. This inconsistency should be addressed in a follow-up (requires a `CurrencyService.Convert` call on the estimate value, which is an additional latency hit).

**Timeout interaction with page latency.** The `getShippingEstimate` call has a 300ms deadline. If called sequentially after the product fetch, it adds up to 300ms to the critical path on a slow `ShippingService` response. This call should be made concurrently with the recommendations fetch using a goroutine + errgroup pattern to prevent it from blocking page render.

**`SHIPPING_SERVICE_ADDR` missing at startup.** If the manifest change is not deployed before the frontend binary change, `mustConnGRPC` will fail at startup and crash the frontend pod. Deployment order matters: manifest first, then binary.
