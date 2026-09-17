-- Curated's own transform: adds a VAT-inclusive total on top of orders_raw's
-- (already deduplicated by raw's own unique gate) rows. Deliberately a real
-- computed column, not a 1:1 copy - proving the transform hop does
-- something, not just relaying rows through.
--
-- Owns creating fct_orders for the same reason build_raw_orders.sql owns
-- orders_raw/orders_raw_quarantine: no dbt project backs this pipeline.
-- TRUNCATE + INSERT for the same idempotency reason as every other
-- procedure in this repo.

CREATE OR REPLACE PROCEDURE build_curated_orders()
LANGUAGE plpgsql
AS $$
DECLARE
    vat_rate CONSTANT numeric := 0.10;
BEGIN
    CREATE TABLE IF NOT EXISTS fct_orders (
        order_id bigint,
        customer text,
        amount numeric,
        amount_with_vat numeric,
        order_date date
    );

    TRUNCATE TABLE fct_orders;

    INSERT INTO fct_orders (order_id, customer, amount, amount_with_vat, order_date)
    SELECT order_id, customer, amount, round(amount * (1 + vat_rate), 2), order_date
    FROM orders_raw;
END;
$$;
