-- NOT from HG - the gold hop: aggregates the silver model through ref().
select count(*)                              as artists
     , count(distinct platform_name)         as distinct_names
     , min(platform_id::bigint)              as min_platform_id
     , max(platform_id::bigint)              as max_platform_id
from {{ ref('dim_artist_active') }}
