-- ml.plan_storage — billing_plan_id → storage cap (GB). Seed/lookup table.
--
-- The admin FS API does NOT expose a per-user quota (only used bytes = root.size),
-- so the cap comes from the plan. Mapping = revenue_facts price catalog cross-
-- referenced with Seedr public pricing (seedr.cc/pricing):
--   Basic  $6.95/mo · $69.5/yr  → 30 GB
--   Pro    $9.95/mo · $99.5/yr  → 100 GB
--   Master $19.95/mo · $199.5/yr→ 1000 GB (1 TB)
--   Free base → 2 GB
-- Plan IDs 1/2=Basic, 3/4=Pro, 5/6=Master cover ~93% of the scored base.
--
-- TODO (billing team): fill the remaining ~7% — regional/newer plan ids
-- (12, 24, 10, 14, 20, 402, 404, 13, 15, 16, 1xxx-series) by price, and confirm
-- Gold/Power tiers (up to 10 TB). Add rows here; user_storage_quota picks them up.
-- Re-run this file to (re)seed; it is NOT rebuilt by snapshot_daily.sh so manual
-- additions persist.

CREATE TABLE IF NOT EXISTS ml.plan_storage (
    billing_plan_id UInt32,
    storage_gb      UInt32,
    plan_name       String,
    confidence      LowCardinality(String)   -- 'confident' | 'base' | 'estimate'
) ENGINE = MergeTree ORDER BY billing_plan_id;

TRUNCATE TABLE ml.plan_storage;
INSERT INTO ml.plan_storage VALUES
    (0,    2, 'Free',          'base'),
    (1,   30, 'Basic',         'confident'),
    (2,   30, 'Basic-annual',  'confident'),
    (3,  100, 'Pro',           'confident'),
    (4,  100, 'Pro-annual',    'confident'),
    (5, 1000, 'Master',        'confident'),
    (6, 1000, 'Master-annual', 'confident');
