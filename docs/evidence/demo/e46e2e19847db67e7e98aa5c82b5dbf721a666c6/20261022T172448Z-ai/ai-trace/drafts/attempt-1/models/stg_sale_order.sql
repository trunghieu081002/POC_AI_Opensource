{{ config(materialized='table', schema='fulfilment_sla') }}

select
    id,
    name,
    state,
    date_order,
    commitment_date
from fulfilment_sla_landing.sale_order
