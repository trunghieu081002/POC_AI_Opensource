{{ config(materialized='table') }}

select distinct
    upper(trim(cast(hg_stock_id as text))) as hg_stock_id
from {{ ref('manual_excluded_stock_codes') }}
where nullif(trim(cast(hg_stock_id as text)), '') is not null