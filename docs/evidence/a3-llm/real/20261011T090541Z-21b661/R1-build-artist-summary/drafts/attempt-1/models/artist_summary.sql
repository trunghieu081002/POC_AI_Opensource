{{ config(materialized='table', schema='a3_artist_summary') }}

with filtered_partners as (
    select
        id,
        trim(name) as clean_name
    from a3_artist_summary_landing.res_partner
    where id is not null
      and id not in (2, 4)
      and name is not null
      and trim(name) <> ''
),

distinct_partners as (
    select
        id,
        min(clean_name) as clean_name
    from filtered_partners
    group by id
)

select
    count(distinct id) as artists,
    count(distinct clean_name) as distinct_names,
    min(id) as min_id,
    max(id) as max_id
from distinct_partners
