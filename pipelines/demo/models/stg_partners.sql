-- landing -> raw, 1:1-shaped: cast + dedupe only, no business logic (that
-- is what gates/procedures are for - docs/layer2.md, Concepts #2/#3).
-- distinct on (id) ... order by write_date desc keeps the latest version
-- of a row a naive (non-incremental) extract could have landed more than
-- once - Odoo's own retroactive write_date is exactly why this is needed
-- for this source (docs/layer2.md, "Why Odoo is the reference source").
--
-- schema: 'demo' matches this pipeline's own warehouse.schema in
-- pipeline.yaml - the project's generate_schema_name macro
-- (deploy.install_dbt_models) returns it verbatim, not concatenated with
-- the target's own default schema.
{{ config(materialized='table', schema='demo') }}

select distinct on (id)
    id::bigint as id,
    name::text as name,
    company_id::bigint as company_id,
    write_date::timestamp as write_date
from demo_landing.res_partner
order by id, write_date desc
