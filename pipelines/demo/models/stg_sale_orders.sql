-- See stg_partners.sql for the dedupe/schema rationale - identical here.
{{ config(materialized='table', schema='demo') }}

select distinct on (id)
    id::bigint as id,
    partner_id::bigint as partner_id,
    company_id::bigint as company_id,
    amount_total::numeric as amount_total,
    currency_id::bigint as currency_id,
    write_date::timestamp as write_date
from demo_landing.sale_order
order by id, write_date desc
