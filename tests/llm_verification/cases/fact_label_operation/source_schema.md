Source: PostgreSQL (Odoo), connector `odoo_postgres`. Two tables are needed; only the columns below exist in
this feed.

`public.x_music_song` — one row per song (recording)

| column            | type             | meaning |
|-------------------|------------------|---------|
| id                | bigint           | song id; primary key (never NULL, never repeated) |
| active            | boolean          | Odoo archive flag; may be NULL |
| isrc              | text             | ISRC code; may be NULL, empty, whitespace only, or carry leading/trailing spaces; several songs may share one |
| release_state     | text             | release status (e.g. released, draft); may be NULL, empty or padded |
| release_date      | date             | release date; may be NULL |
| distro            | double precision | distributor id (whole numbers stored as double); may be NULL |
| purchase_order_id | double precision | the purchase order the song was bought under (whole numbers stored as double); may be NULL |
| name              | text             | song title - not used by this report |

`public.x_acceptance_cert` — acceptance certificates of purchase orders

| column            | type             | meaning |
|-------------------|------------------|---------|
| id                | bigint           | certificate id; primary key |
| purchase_order_id | double precision | the purchase order it certifies (whole numbers stored as double); never NULL |
| acceptance_link   | text             | link to the certificate document; may be NULL, empty or padded |
| name              | text             | certificate title - not used by this report |

Landing: dpagent's extract copies both tables as received (nothing dropped, nothing cast) into the landing dataset
named by the manifest's `landing_dataset_name`. HG's convention, which the dbt project template follows, is the
schema `staging`: set `landing_dataset_name: staging`; the extracted tables are `staging.x_music_song` and
`staging.x_acceptance_cert`, read in dbt as `source('staging', '<table>')`. The output table lives in the schema
`silver` (the pipeline's `warehouse.schema`).
