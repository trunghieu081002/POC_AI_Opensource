-- No write_date on this table (see pipeline.yaml's landing schema_contract)
-- to dedupe by, unlike stg_partners/stg_sale_orders - `order by id` alone
-- still makes distinct on (id) deterministic, just not "latest wins".
{{ config(materialized='table', schema='demo') }}

select distinct on (id)
    id::bigint as id,
    order_id::bigint as order_id,
    product_id::bigint as product_id,
    price_subtotal::numeric as price_subtotal
from demo_landing.sale_order_line
order by id
