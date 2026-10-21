Source: **MongoDB 7** (the order service's own database), collection `orders`. There is no SQL replica of this
data. One document per sales order; deliveries are embedded.

```json
{
  "_id": 42,                                  // order id (int64), unique
  "name": "S00042",
  "state": "sale",                            // draft | sent | sale | done | cancel
  "date_order": {"$date": "2026-01-05T10:00:00Z"},
  "commitment_date": {"$date": "2026-01-08T18:00:00Z"},   // may be missing or null
  "pickings": [                               // may be empty
    {"id": 101, "picking_type": "outgoing", "state": "done", "date_done": {"$date": "2026-01-07T15:00:00Z"}}
  ]
}
```

Connection: a MongoDB connection string (`mongodb://…`), read-only user.
