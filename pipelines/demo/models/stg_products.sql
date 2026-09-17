-- See stg_sale_order_lines.sql - same no-write_date dedupe rationale.
{{ config(materialized='table', schema='demo') }}

select distinct on (id)
    id::bigint as id,
    name::text as name
from demo_landing.product_template
order by id
