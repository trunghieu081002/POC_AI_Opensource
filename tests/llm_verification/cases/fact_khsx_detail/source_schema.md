Source: PostgreSQL (Odoo), connector `odoo_postgres`, table `public.x_music_plan_detail` (the only
table needed). Only the columns below exist in this feed.

| column             | type   | meaning |
|--------------------|--------|---------|
| id                 | bigint | production-plan detail id; primary key (never NULL, never repeated) |
| plan_id            | bigint | the production plan this detail belongs to; may be NULL |
| produce_manager    | bigint | user id of the production manager; may be NULL |
| ar_id              | bigint | user id of the A&R person; may be NULL |
| state              | text   | workflow state (e.g. draft, approved, cancel); may be NULL, empty, whitespace only, or carry leading/trailing spaces |
| quality            | text   | quality grade - not used by this report |
| original_song_name | text   | free text - not used by this report |

Landing: dpagent's extract copies the table as received (nothing dropped, nothing cast) into the
landing dataset named by the manifest's `landing_dataset_name`. HG's convention, which the dbt
project template follows, is the schema `staging`: set `landing_dataset_name: staging`, and the
extracted table is `staging.x_music_plan_detail`, read in dbt as `source('staging', 'x_music_plan_detail')`.
The output table lives in the schema `silver` (the pipeline's `warehouse.schema`).
