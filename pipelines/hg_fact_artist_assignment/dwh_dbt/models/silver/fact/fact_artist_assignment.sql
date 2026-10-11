-- silver.fact_artist_assignment  (target theo Data Dictionary)
{{ config(materialized='table') }}

select
    {{ dbt_utils.generate_surrogate_key(['x.id']) }} as fact_artist_assignment_sk
    , nullif(trim(cast(x.detail_id as text)),'') as production_plan_detail_id
    , nullif(trim(cast(x.partner_id as text)),'') as artist_id
    , nullif(trim(rp.name),'') as artist_name
from {{ source('staging','x_music_plan_detail_price') }} x
left join {{ source('staging','res_partner') }} rp on x.partner_id = rp.id

-- left join {{ source('staging','res_partner') }} on ...  -- JOIN qua partner_id
