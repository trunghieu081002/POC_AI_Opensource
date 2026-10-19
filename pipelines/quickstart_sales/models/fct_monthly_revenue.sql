-- Monthly revenue and number of orders, completed orders only (status 'done'); other statuses are
-- not revenue. Full snapshot: the table is rebuilt each run, so a rerun never duplicates a month.
{{ config(materialized='table', schema='quickstart_sales') }}

select
    order_month                      as month,
    count(*)::bigint                 as orders,
    sum(amount)::numeric(20, 2)      as revenue
from quickstart_sales.stg_orders
where status = 'done'
group by order_month
