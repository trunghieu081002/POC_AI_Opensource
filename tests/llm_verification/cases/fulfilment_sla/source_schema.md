Source: PostgreSQL (Odoo), connector `odoo_postgres`. Two tables are needed; only the columns below exist in
this feed. Timestamps are `timestamp without time zone` holding UTC.

`public.sale_order` — one row per sales order

| column          | type      | meaning |
|-----------------|-----------|---------|
| id              | bigint    | order id; primary key (never NULL, never repeated) |
| name            | text      | order reference (e.g. S00042) - not used by this report |
| state           | text      | `draft`, `sent`, `sale`, `done` or `cancel`; never NULL |
| date_order      | timestamp | when the order was placed (UTC); never NULL |
| commitment_date | timestamp | promised delivery time (UTC); may be NULL |

`public.stock_picking` — one row per stock transfer

| column       | type      | meaning |
|--------------|-----------|---------|
| id           | bigint    | transfer id; primary key |
| sale_id      | bigint    | the sales order it belongs to; may be NULL |
| picking_type | text      | `outgoing` (delivery to the customer), `incoming` (return), `internal`; never NULL |
| state        | text      | `draft`, `assigned`, `done` or `cancel`; never NULL |
| date_done    | timestamp | when the transfer was completed (UTC); NULL unless `state` is `done` |
