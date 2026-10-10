Source: PostgreSQL, connector `odoo_postgres`, table `public.res_partner` (the only table needed).

| column | type   | meaning |
|--------|--------|---------|
| id     | bigint | partner id. In real Odoo this is the primary key; this feed may nevertheless repeat a row (the same id twice) |
| name   | text   | partner display name; may be NULL, empty, or whitespace only; may carry leading/trailing spaces |

Landing: dpagent's extract copies the table as received (nothing dropped, nothing cast) into the
schema `a3_artist_summary_landing`, so the extracted table is `a3_artist_summary_landing.res_partner`.
The pipeline's own warehouse schema (for its dbt models / output tables) is `a3_artist_summary`.
