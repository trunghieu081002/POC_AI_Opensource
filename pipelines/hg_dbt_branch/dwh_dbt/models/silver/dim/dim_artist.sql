-- silver.dim_artist  (target theo Data Dictionary)
select
    {{ dbt_utils.generate_surrogate_key(['id']) }} as dim_artist_sk
    , nullif(trim(cast(id as text)),'') as platform_id
    , nullif(trim(cast(name as text)),'') as platform_name
from {{ source('staging', 'res_partner') }}
