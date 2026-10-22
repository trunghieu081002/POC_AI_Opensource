-- Hand-written reference (ORIGIN.md). BRD: eligible = state sale|done; a delivery = outgoing + done; the first
-- delivery decides; due = commitment_date or date_order + 72 h; on time = delivered at or before due; by order month.
{{ config(materialized='table', schema='fulfilment_sla') }}
with orders as (
    select id, date_order, coalesce(commitment_date, date_order + interval '72 hours') as due
    from fulfilment_sla_landing.sale_order
    where state in ('sale', 'done')
), first_delivery as (
    select sale_id, min(date_done) as delivered_at
    from fulfilment_sla_landing.stock_picking
    where picking_type = 'outgoing' and state = 'done' and sale_id is not null and date_done is not null
    group by sale_id
)
select to_char(o.date_order, 'YYYY-MM') as month,
       count(*)::bigint as eligible_orders,
       count(d.delivered_at)::bigint as delivered_orders,
       count(*) filter (where d.delivered_at <= o.due)::bigint as on_time_orders,
       round(100.0 * count(*) filter (where d.delivered_at <= o.due) / count(*), 1) as on_time_rate
from orders o
left join first_delivery d on d.sale_id = o.id
group by 1
