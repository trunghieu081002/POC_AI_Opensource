-- NOT from HG - written for this branch to exercise the constructs HG relies on
-- that dim_artist alone does not: ref() to another model, and a seed
-- (HG's int_excluded_stock_codes does the same with its own seed).
select a.dim_artist_sk
     , a.platform_id
     , a.platform_name
from {{ ref('dim_artist') }} a
left join {{ ref('manual_excluded_partner_ids') }} x
       on x.platform_id = a.platform_id
where x.platform_id is null
