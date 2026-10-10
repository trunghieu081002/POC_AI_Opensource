Source: PostgreSQL, connector `odoo_postgres`, table `public.sale_order`.

| column           | type      | meaning |
|------------------|-----------|---------|
| id               | bigint    | order id |
| date_order       | timestamp | when the quotation/order was created |
| confirmation_date| timestamp | when the order was confirmed by sales (NULL while still a quotation) |
| state            | text      | one of: draft, sent, sale, done, cancel |
| amount_untaxed   | numeric   | total before tax |
| amount_tax       | numeric   | tax amount |
| amount_total     | numeric   | total including tax |
| currency_id      | bigint    | order currency (several currencies are in use) |

Landing: dpagent copies the table as received into the schema `a3_revenue_ambiguous_landing`
(table `a3_revenue_ambiguous_landing.sale_order`). The pipeline's own warehouse schema is `a3_revenue_ambiguous`.
