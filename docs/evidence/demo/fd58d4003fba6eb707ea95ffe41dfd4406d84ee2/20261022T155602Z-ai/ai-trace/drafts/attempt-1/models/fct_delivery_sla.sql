{{ config(materialized='table', schema='fulfilment_sla') }}

with deliveries as (
    select
        sale_id,
        min(date_done) as first_delivery_date
    from fulfilment_sla.stg_stock_pickings
    where sale_id is not null
      and picking_type = 'outgoing'
      and state = 'done'
    group by sale_id
),

order_sla as (
    select
        o.id,
        to_char(o.date_order, 'YYYY-MM') as month,
        (d.first_delivery_date is not null) as is_delivered,
        (d.first_delivery_date is not null
         and d.first_delivery_date <= coalesce(o.commitment_date, o.date_order + interval '3 days')
        ) as is_on_time
    from fulfilment_sla.stg_sale_orders o
    left join deliveries d
        on o.id = d.sale_id
    where o.state in ('sale', 'done')
)

select
    month,
    count(*)::bigint as eligible_orders,
    count(case when is_delivered then 1 end)::bigint as delivered_orders,
    count(case when is_on_time then 1 end)::bigint as on_time_orders,
    round(
        (count(case when is_on_time then 1 end)::numeric / count(*)::numeric) * 100,
        1
    ) as on_time_rate
from order_sla
group by month
order by month
