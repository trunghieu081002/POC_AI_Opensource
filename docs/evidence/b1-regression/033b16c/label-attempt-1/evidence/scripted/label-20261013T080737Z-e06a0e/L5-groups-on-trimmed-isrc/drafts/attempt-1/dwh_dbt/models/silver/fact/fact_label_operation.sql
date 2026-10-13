-- one row per raw ISRC, smallest song id (scripted reference draft, written by a person)
with songs as (
    select *
    from {{ source('staging', 'x_music_song') }}
    where active is true
      and nullif(trim(isrc), '') is not null
),
ranked as (
    select s.*, c.acceptance_link,
           row_number() over (partition by trim(s.isrc) order by s.id) as rn
    from songs s
    left join {{ source('staging', 'x_acceptance_cert') }} c
           on c.purchase_order_id::bigint = s.purchase_order_id::bigint
)
select
    {{ dbt_utils.generate_surrogate_key(['id']) }}          as fact_label_operation_sk,
    nullif(trim(id::text), '')                             as resource_id,
    nullif(trim(release_state), '')                        as release_status,
    nullif(trim(release_date::text), '')::timestamp        as release_date,
    nullif(trim(acceptance_link), '')                      as acceptance_url,
    nullif(trim(isrc), '')                                 as isrc,
    nullif(trim(distro::text), '')                         as distributor
from ranked
where rn = 1
