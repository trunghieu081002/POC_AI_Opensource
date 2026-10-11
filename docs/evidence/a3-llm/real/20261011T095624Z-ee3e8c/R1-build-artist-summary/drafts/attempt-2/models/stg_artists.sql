{{ config(materialized='table', schema='a3_artist_summary') }}

with cleaned as (
    select
        id,
        trim(name) as name
    from a3_artist_summary_landing.res_partner
    where id not in (2, 4)
      and name is not null
      and trim(name) != ''
)
select
    id,
    min(name) as name
from cleaned
group by id
