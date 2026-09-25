-- Curated's transform: a VAT-inclusive total on top of stg_orders (already
-- deduplicated by raw's own unique gate). A procedure here, dbt above - the
-- mix docs/layer2.md describes as the common pattern. TRUNCATE + INSERT for
-- the same idempotency reason as every procedure in this repo.

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
    FROM stg_orders;
END;
$$;
