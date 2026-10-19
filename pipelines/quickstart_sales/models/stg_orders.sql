-- One row per source order, typed: the month it was placed and its status (trimmed, lower case).
-- Reads landing by its literal schema-qualified name (the shared-project convention, docs/layer2.md).
{{ config(materialized='table', schema='quickstart_sales') }}

select
    id::bigint                       as id,
    to_char(order_date, 'YYYY-MM')   as order_month,
    lower(trim(status))              as status,
    amount::numeric(20, 2)           as amount
from quickstart_sales_landing.orders
