{{ config(materialized='table', schema='a3_artist_summary') }}

select
    count(distinct id) as artists,
    count(distinct name) as distinct_names,
    min(id) as min_id,
    max(id) as max_id
from a3_artist_summary.stg_artists
