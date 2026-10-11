{{ config(materialized='table', schema='a3_artist_summary') }}
with qualifying as (
    select distinct id, name as name
    from a3_artist_summary_landing.res_partner
    where id not in (2, 4)
      and name is not null
      and btrim(name) <> ''
)
select count(distinct id)::bigint       as artists,
       count(distinct name)::bigint     as distinct_names,
       min(id)::bigint                  as min_id,
       max(id)::bigint                  as max_id
from qualifying
