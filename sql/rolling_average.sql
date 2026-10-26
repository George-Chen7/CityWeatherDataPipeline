-- ============================================================
-- 业务问题 5：计算每个城市每个观测时刻往前 7 小时的平均气温。
-- 考察：窗口函数、PARTITION BY
--
-- 口径说明：
--   数据是逐小时的，「往前 7 小时」= 当前整点 + 前面 6 个整点，共 7 行，
--   对应窗口 ROWS BETWEEN 6 PRECEDING AND CURRENT ROW。
--   PARTITION BY city_id 保证窗口不会跨到别的城市；ORDER BY observed_at
--   决定「前 6 行」是哪 6 行，缺了它 ROWS 窗口会按物理顺序取，结果不可靠。
-- ============================================================

SELECT
    c.city_name                                             AS "城市",
    to_char(w.observed_at AT TIME ZONE c.timezone,
            'YYYY-MM-DD HH24:MI')                           AS "观测时刻(当地)",
    w.temperature_c                                         AS "当前气温(℃)",
    ROUND((AVG(w.temperature_c) OVER seven_hours)::numeric, 2)
                                                            AS "7小时滚动平均气温(℃)",
    COUNT(*) OVER seven_hours                               AS "窗口内观测数"
FROM cities AS c
JOIN weather_hourly AS w
    ON c.city_id = w.city_id
WHERE w.quality IN ('valid', 'suspect')
  AND w.temperature_c IS NOT NULL
WINDOW seven_hours AS (
    PARTITION BY w.city_id
    ORDER BY w.observed_at
    ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
)
ORDER BY c.city_name, w.observed_at;

-- 说明：
--   * 每个城市最早 6 个时刻只有 1~6 行可用（窗口还没填满），
--     「窗口内观测数」这一列会如实显示 1,2,...,7，属于正常边界，不是丢数据。
--   * 若某天有缺测（某些整点根本不存在），按行数的 ROWS 窗口会把更早的时刻拉进来，
--     这时应按时间跨度开窗：
--         ORDER BY w.observed_at
--         RANGE BETWEEN INTERVAL '6 hours' PRECEDING AND CURRENT ROW
--     RANGE 版本在数据完整时与 ROWS 版本结果一致。
--   * 只想看某个城市/某几天时，直接在最外层加条件即可，例如
--         WHERE c.city_name = '深圳'
--           AND (w.observed_at AT TIME ZONE c.timezone)::date
--               BETWEEN DATE '2026-10-15' AND DATE '2026-10-16'
--     注意：外层 WHERE 在开窗前生效，窗口只在这个子集里滚动，
--     所以子集开头几行同样是「不足 7 行」的边界。
