-- Curated's transform: revenue per month, summed from raw's own already-
-- deduplicated (unique gate) stg_sale_order. CREATE TABLE IF NOT EXISTS +
-- TRUNCATE + INSERT - never CREATE TABLE AS/DROP - the same idempotency
-- convention every procedure in this repo already uses
-- (pipelines/quickstart_dbt/procedures/build_curated_orders.sql's own
-- comment): a second run of this exact procedure against the exact same
-- raw data must produce the exact same curated rows, not duplicate them -
-- directly what M2.5's own "Hai lượt Airflow" acceptance case proves.
CREATE OR REPLACE PROCEDURE build_monthly_sales()
LANGUAGE plpgsql
AS $$
BEGIN
    CREATE TABLE IF NOT EXISTS fct_monthly_sales (
        month text,
        revenue numeric
    );

    TRUNCATE TABLE fct_monthly_sales;

    INSERT INTO fct_monthly_sales (month, revenue)
    SELECT order_month, SUM(amount_total)
    FROM stg_sale_order
    GROUP BY order_month;
END;
$$;
