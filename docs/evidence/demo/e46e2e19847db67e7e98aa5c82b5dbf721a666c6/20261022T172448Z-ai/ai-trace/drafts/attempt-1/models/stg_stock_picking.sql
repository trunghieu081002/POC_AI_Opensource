{{ config(materialized='table', schema='fulfilment_sla') }}

select
    id,
    sale_id,
    picking_type,
    state,
    date_done
from fulfilment_sla_landing.stock_picking
