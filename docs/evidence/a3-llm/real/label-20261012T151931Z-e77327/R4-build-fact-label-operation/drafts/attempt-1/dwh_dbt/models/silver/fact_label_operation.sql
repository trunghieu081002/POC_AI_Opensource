with filtered_songs as (
    select
        id,
        active,
        isrc,
        release_state,
        release_date,
        distro,
        purchase_order_id,
        row_number() over (
            partition by isrc
            order by id asc
        ) as row_num
    from {{ source('staging', 'x_music_song') }}
    where active is true
      and trim(isrc) is not null
      and trim(isrc) != ''
),

deduplicated_songs as (
    select
        id,
        isrc,
        release_state,
        release_date,
        distro,
        purchase_order_id
    from filtered_songs
    where row_num = 1
),

certs as (
    select
        cast(purchase_order_id as bigint) as purchase_order_id,
        acceptance_link
    from {{ source('staging', 'x_acceptance_cert') }}
),

final as (
    select
        {{ dbt_utils.generate_surrogate_key(['s.id']) }} as fact_label_operation_sk,
        cast(s.id as text) as resource_id,
        nullif(trim(s.release_state), '') as release_status,
        cast(s.release_date as timestamp) as release_date,
        nullif(trim(c.acceptance_link), '') as acceptance_url,
        nullif(trim(s.isrc), '') as isrc,
        case
            when s.distro is not null then cast(cast(s.distro as bigint) as text)
            else null
        end as distributor
    from deduplicated_songs s
    left join certs c
        on cast(s.purchase_order_id as bigint) = c.purchase_order_id
)

select
    fact_label_operation_sk,
    resource_id,
    release_status,
    release_date,
    acceptance_url,
    isrc,
    distributor
from final
