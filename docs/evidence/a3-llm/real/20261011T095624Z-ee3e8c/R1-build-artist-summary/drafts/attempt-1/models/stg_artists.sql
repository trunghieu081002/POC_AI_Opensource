{{ config(materialized='table', schema='a3_artist_summary') }}

with filtered as (
    select
        id,
        trim(name) as name,
        row_number() over (partition by id order by name) as rn
    from a3_artist_summary_landing.res_partner
    where id not in (2, 4)
      and name is not null
      and trim(name) != ''
)
select
    id,
    name
from filtered
where rn = 1
