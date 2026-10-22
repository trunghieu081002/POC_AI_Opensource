{{ config(materialized='table', schema='fulfilment_sla') }}

with first_deliveries as (
    select
        sale_id,
        min(date_done) as first_delivery_date
    from fulfilment_sla.stg_stock_picking
    where picking_type = 'outgoing'
      and state = 'done'
      and sale_id is not null
    group by sale_id
)

select
    to_char(so.date_order, 'YYYY-MM') as month,
    count(*)::bigint as eligible_orders,
    count(case when del.first_delivery_date is not null then 1 end)::bigint as delivered_orders,
    count(case when del.first_delivery_date is not null and del.first_delivery_date <= coalesce(so.commitment_date, so.date_order + interval '3 days') then 1 end)::bigint as on_time_orders,
    round(count(case when del.first_delivery_date is not null and del.first_delivery_date <= coalesce(so.commitment_date, so.date_order + interval '3 days') then 1 end) * 100.0 / count(*), 1) as on_time_rate
from fulfilment_sla.stg_sale_order so
left join first_deliveries del
    on so.id = del.sale_id
where so.state in ('sale', 'done')
group by to_char(so.date_order, 'YYYY-MM')
order by month
