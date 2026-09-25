-- Casts landing's uncast CSV columns. `schema='quickstart_dbt'` lands it in the
-- pipeline's own warehouse.schema (deploy.install_dbt_models writes the
-- generate_schema_name macro override that makes dbt use it exactly, instead of
-- concatenating it onto the target schema). materialized='table' drops and
-- recreates it every run, so a re-run reproduces it rather than adding to it.
{{ config(materialized='table', schema='quickstart_dbt') }}

select
    order_id::bigint   as order_id,
    customer           as customer,
    amount::numeric    as amount,
    order_date::date   as order_date
from quickstart_dbt_landing.orders
