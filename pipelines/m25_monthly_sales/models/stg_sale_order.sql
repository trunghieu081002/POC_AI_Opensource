-- Casts + derives the only two things curated needs: a YYYY-MM order month
-- (from write_date) and amount_total, both already numeric/timestamp in
-- landing (dlt's own as-received snapshot, Postgres types matching
-- whatever seed_source()/fixture.yaml declared for the throwaway source
-- table). `schema='m25_monthly_sales'` lands this in the pipeline's own
-- warehouse.schema exactly (deploy.install_dbt_models()'s own
-- generate_schema_name macro override - the same convention
-- pipelines/quickstart_dbt/models/stg_orders.sql's own comment documents).
-- Reads landing by its literal, schema-qualified name directly, never a
-- dbt cross-model Jinja call - the convention
-- validate.check_dbt_dependencies() enforces for every model in this
-- project.
{{ config(materialized='table', schema='m25_monthly_sales') }}

select
    id::bigint                     as id,
    to_char(write_date, 'YYYY-MM') as order_month,
    amount_total::numeric          as amount_total
from m25_monthly_sales_landing.sale_order
