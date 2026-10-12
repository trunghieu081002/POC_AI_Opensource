-- one row per production-plan detail (scripted reference draft, written by a person)
select
    {{ dbt_utils.generate_surrogate_key(['id']) }}       as fact_khsx_detail_sk,
    nullif(trim(id::text), '')                          as production_plan_detail_id,
    nullif(trim(plan_id::text), '')                     as production_plan_id,
    nullif(trim(produce_manager::text), '')             as production_manager,
    nullif(trim(ar_id::text), '')                       as ar_manager,
    nullif(trim(state), '')                             as status
from {{ source('staging', 'x_music_plan_detail') }}
