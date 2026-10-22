{{ config(materialized='table', schema='fulfilment_sla') }}

with eligible_orders as (
    select
        id as order_id,
        to_char(date_order, 'YYYY-MM') as month,
        date_order,
        coalesce(commitment_date, date_order + interval '3 days') as commitment_deadline
    from fulfilment_sla.stg_sale_order
    where state in ('sale', 'done')
),

deliveries as (
    select
        sale_id,
        min(date_done) as first_delivery_date
    from fulfilment_sla.stg_stock_picking
    where picking_type = 'outgoing'
      and state = 'done'
      and sale_id is not null
    group by sale_id
),

order_status as (
    select
        o.order_id,
        o.month,
        o.commitment_deadline,
        d.first_delivery_date,
        case
            when d.first_delivery_date is not null then 1
            else 0
        end as is_delivered,
        case
            when d.first_delivery_date is not null and d.first_delivery_date <= o.commitment_deadline then 1
            else 0
        end as is_on_time
    from eligible_orders o
    left join deliveries d on o.order_id = d.sale_id
)

select
    month::text as month,
    count(*)::bigint as eligible_orders,
    sum(is_delivered)::bigint as delivered_orders,
    sum(is_on_time)::bigint as on_time_orders,
    round((sum(is_on_time)::numeric / count(*)::numeric) * 100, 1)::numeric as on_time_rate
from order_status
group by month
order by month
