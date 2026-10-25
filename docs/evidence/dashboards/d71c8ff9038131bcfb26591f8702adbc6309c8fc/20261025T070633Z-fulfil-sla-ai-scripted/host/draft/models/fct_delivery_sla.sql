{{ config(materialized='table', schema='fulfilment_sla') }}

WITH valid_deliveries AS (
    SELECT
        sale_id,
        MIN(date_done) AS first_delivery_date
    FROM fulfilment_sla_landing.stock_picking
    WHERE picking_type = 'outgoing'
      AND state = 'done'
      AND sale_id IS NOT NULL
    GROUP BY sale_id
),

eligible_orders AS (
    SELECT
        so.id,
        to_char(so.date_order, 'YYYY-MM') AS month,
        so.date_order,
        COALESCE(so.commitment_date, so.date_order + INTERVAL '3 days') AS commitment_deadline,
        vd.first_delivery_date
    FROM fulfilment_sla_landing.sale_order AS so
    LEFT JOIN valid_deliveries AS vd
        ON so.id = vd.sale_id
    WHERE so.state IN ('sale', 'done')
)

SELECT
    month,
    COUNT(*)::bigint AS eligible_orders,
    COUNT(first_delivery_date)::bigint AS delivered_orders,
    COUNT(
        CASE
            WHEN first_delivery_date IS NOT NULL
                 AND first_delivery_date <= commitment_deadline
            THEN 1
        END
    )::bigint AS on_time_orders,
    ROUND(
        (
            COUNT(
                CASE
                    WHEN first_delivery_date IS NOT NULL
                         AND first_delivery_date <= commitment_deadline
                    THEN 1
                END
            )::numeric
            / COUNT(*)::numeric
        ) * 100,
        1
    )::numeric AS on_time_rate
FROM eligible_orders
GROUP BY month
ORDER BY month ASC
