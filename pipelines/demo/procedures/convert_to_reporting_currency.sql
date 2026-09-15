-- Converts every stg_sale_orders row into fct_sales, in the reporting
-- currency (USD), looking up the exchange rate for the order's own
-- write_date with a fallback: if that day has no rate (a real gap in
-- exchange_rates - weekends, a missed feed), walk backward up to 5 days.
-- Orders still unconverted after that land in fct_sales_quarantine instead
-- of fct_sales, with the reason recorded.
--
-- Why this is a procedure and not a dbt model (docs/layer2.md, Concepts #3):
-- the fallback is a per-row, iterative lookup ("try today, then yesterday,
-- then the day before, stop at 5") - not something a single declarative
-- `select` expresses cleanly. A dbt model checking exchange_rates for an
-- exact date match would just silently drop every order missing that
-- day's rate, which is precisely the kind of wrong-number-that-looks-fine
-- this whole design exists to prevent.
--
-- Idempotency is this file's own responsibility, same as any pack step's
-- guard (docs/layer2.md, Concepts #3, "idempotency is the author's
-- responsibility, not something the generator proves for you"): TRUNCATE
-- + INSERT makes a re-run of the same raw data produce the same curated
-- data, never an accumulating duplicate.

CREATE OR REPLACE PROCEDURE convert_to_reporting_currency()
LANGUAGE plpgsql
AS $$
DECLARE
    rpt_currency CONSTANT text := 'USD';
    max_lookback CONSTANT int := 5;
    r RECORD;
    rate numeric;
    days_back int;
BEGIN
    TRUNCATE TABLE fct_sales;

    FOR r IN SELECT * FROM stg_sale_orders LOOP
        rate := NULL;
        days_back := 0;

        WHILE rate IS NULL AND days_back <= max_lookback LOOP
            SELECT er.rate INTO rate
            FROM exchange_rates er
            WHERE er.currency_id = r.currency_id
              AND er.rate_date = (r.write_date::date - days_back);

            IF rate IS NULL THEN
                days_back := days_back + 1;
            END IF;
        END LOOP;

        IF rate IS NULL THEN
            INSERT INTO fct_sales_quarantine (order_id, currency_id, write_date, reason)
            VALUES (r.id, r.currency_id, r.write_date,
                    format('no exchange rate for currency_id=%s within %s days of %s',
                           r.currency_id, max_lookback, r.write_date::date));
        ELSE
            INSERT INTO fct_sales (order_id, partner_id, currency_id,
                                    amount_total, converted_amount, reporting_currency)
            VALUES (r.id, r.partner_id, r.currency_id,
                    r.amount_total, r.amount_total * rate, rpt_currency);
        END IF;
    END LOOP;
END;
$$;
