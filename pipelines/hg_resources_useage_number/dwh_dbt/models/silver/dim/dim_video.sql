{{ config(materialized='table') }}

with channel_master as (
    select
        nullif(trim(ch."YoutubeChannelId"), '') as channel_id
    from {{ source('staging', 'channel') }} ch
    where nullif(trim(ch."YoutubeChannelId"), '') is not null
      and exists (
          select 1
          from {{ source('staging', 'channel_company') }} cc
          where cc."ChannelId" = ch."Id"
            and coalesce(cc."IsDeleted", false) = false
            and cc."CompanyId" = '{{ var("target_company_id", "448025a0-0ff7-4ed0-a917-399a73decef2") }}'
      )
),

source_data as (
    select
        cvi.*,
        row_number() over (
            partition by cvi."YoutubeVideoId"
            order by cvi."PublishedAt" desc
        ) as rn
    from {{ source('staging', 'channel_video_info') }} cvi
    join channel_master cm
        on nullif(trim(cvi."YoutubeChannelId"), '') = cm.channel_id
    where nullif(trim(cvi."YoutubeVideoId"), '') is not null
)

select
    {{ dbt_utils.generate_surrogate_key(['"YoutubeVideoId"']) }} as dim_video_sk,
    nullif(trim("YoutubeVideoId"), '') as video_id,
    nullif(trim("YoutubeChannelId"), '') as channel_id,
    'https://www.youtube.com/watch?v=' || "YoutubeVideoId" as video_url,
    nullif(trim("Code"), '') as editing_code,
    cast("PublishedAt" as timestamp) as published_date,
    nullif(trim("Title"), '') as video_name
from source_data
where rn = 1