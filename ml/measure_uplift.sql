-- Campaign uplift measurement (Phase 3b) — run AFTER a campaign has been sent
-- to arm='treatment' AND the label window (default 14d) has elapsed.
--
-- Uplift = conv(treatment) - conv(holdout) is the CAUSAL effect of the email
-- (holdout was deliberately NOT emailed). A positive, significant gap means the
-- email actually moved people, not just that high-scorers convert anyway.
--
-- "Conversion" = a COMPLETED payment in (assigned_date, assigned_date + WINDOW d].
-- Source: seedr_telemetry.revenue_facts (user_id, amount_usd, transaction_date, status).
-- For hd_upsell (true plan upgrade) add: AND r.billing_plan_id IN (<higher tiers>).
--
-- ONE statement (CH HTTP rejects multi-statement) → returns a single summary row
-- with both arms, uplift, ARPU, and a two-proportion z-test verdict.
--
-- Prereqs: re-run ml/assign_holdout.py RIGHT BEFORE sending so assigned_date =
-- send date (the window is measured from assigned_date); email sent to
-- arm='treatment' only; >= WINDOW days since assigned_date.
-- (Running this before a send shows the baseline arm balance — uplift should be
-- ~0 / not_significant then; a sanity check that randomization is clean.)
--
-- Run (tunnel up). Placeholders CAMPAIGN / WINDOW filled with sed:
--   F=ml/measure_uplift.sql ; CAMP=hd_upsell ; W=14
--   CRED=~/.clickhouse.seedr ; U=$(grep ^user= $CRED|cut -d= -f2) ; P=$(grep ^password= $CRED|cut -d= -f2)
--   curl -sS "http://127.0.0.1:8123/" --user "$U:$P" --data-binary \
--     "$(sed "s/CAMPAIGN_PH/$CAMP/g; s/WINDOW_PH/$W/g" $F) FORMAT Vertical"

WITH conv AS (
    SELECT h.arm AS arm, h.user_id AS user_id,
           max(if(r.transaction_date > h.assigned_date
                  AND r.transaction_date <= h.assigned_date + INTERVAL WINDOW_PH DAY, 1, 0)) AS converted,
           sumIf(r.amount_usd, r.transaction_date > h.assigned_date
                 AND r.transaction_date <= h.assigned_date + INTERVAL WINDOW_PH DAY)         AS revenue
    FROM ml.campaign_holdout h
    LEFT JOIN (SELECT assumeNotNull(user_id) AS user_id, amount_usd, transaction_date
               FROM seedr_telemetry.revenue_facts WHERE status = 'completed') r
           ON r.user_id = h.user_id
    WHERE h.campaign = 'CAMPAIGN_PH'
    GROUP BY h.arm, h.user_id
),
agg AS (
    SELECT countIf(arm='treatment') AS nt, sumIf(converted, arm='treatment') AS ct,
           round(sumIf(revenue, arm='treatment'), 2) AS rev_t,
           countIf(arm='holdout')   AS nh, sumIf(converted, arm='holdout')   AS ch,
           round(sumIf(revenue, arm='holdout'),   2) AS rev_h
    FROM conv
)
SELECT
    'CAMPAIGN_PH'                                       AS campaign,
    nt AS treat_users, ct AS treat_conv, round(ct/nt, 5) AS conv_treatment,
    rev_t AS treat_revenue_usd, round(rev_t/nt, 4)     AS arpu_treatment,
    nh AS hold_users,  ch AS hold_conv,  round(ch/nh, 5) AS conv_holdout,
    rev_h AS hold_revenue_usd, round(rev_h/nh, 4)      AS arpu_holdout,
    round(ct/nt - ch/nh, 5)                            AS absolute_uplift,
    round((ct/nt - ch/nh) / nullIf(ch/nh, 0) * 100, 1) AS relative_uplift_pct,
    round((ct/nt - ch/nh) /
          sqrt( ((ct+ch)/(nt+nh)) * (1-(ct+ch)/(nt+nh)) * (1.0/nt + 1.0/nh) ), 2) AS z_score,
    if(abs((ct/nt - ch/nh) /
        sqrt( ((ct+ch)/(nt+nh)) * (1-(ct+ch)/(nt+nh)) * (1.0/nt + 1.0/nh) )) > 1.96,
       'significant_95', 'not_significant')            AS verdict
FROM agg
