-- Casts landing's uncast CSV columns into orders_raw, and owns creating
-- both orders_raw and its quarantine table: this pipeline has no dbt models
-- to materialize them the way pipelines/demo's raw stage does (its engine
-- is dbt), so the procedure engine's own SQL is where that DDL has to live.
-- IF NOT EXISTS makes a re-run on an already-provisioned warehouse a no-op
-- here, same idempotency contract as everywhere else in this stage.
--
-- orders_raw_quarantine has to be exactly orders_raw's own columns plus a
-- trailing `reason` (runtime._quarantine_sql's own INSERT is a positional
-- `SELECT *, '<reason>'` against orders_raw) - kept in lockstep by hand,
-- same as pipelines/demo's fct_sales_quarantine. The optional trailing
-- `dpagent_run_id` opts in to the runtime stamping which run quarantined a
-- row (quarantine accumulates across runs; without it repeats are ambiguous).
--
-- TRUNCATE + INSERT makes a re-run of the same landing data produce the
-- same orders_raw, never an accumulating duplicate (docs/layer2.md,
-- Concepts #3: idempotency is the author's responsibility).

CREATE OR REPLACE PROCEDURE build_raw_orders()
LANGUAGE plpgsql
AS $$
BEGIN
    CREATE TABLE IF NOT EXISTS orders_raw (
        order_id bigint,
        customer text,
        amount numeric,
        order_date date
    );
    CREATE TABLE IF NOT EXISTS orders_raw_quarantine (
        order_id bigint,
        customer text,
        amount numeric,
        order_date date,
        reason text,
        dpagent_run_id bigint
    );
    -- A table created before this column existed gets it appended (after
    -- `reason`, which is exactly the trailing pair the runtime looks for).
    ALTER TABLE orders_raw_quarantine ADD COLUMN IF NOT EXISTS dpagent_run_id bigint;

    TRUNCATE TABLE orders_raw;

    INSERT INTO orders_raw (order_id, customer, amount, order_date)
    SELECT order_id::bigint, customer, amount::numeric, order_date::date
    FROM quickstart_landing.orders;
END;
$$;
