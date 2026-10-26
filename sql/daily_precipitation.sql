-- ============================================================
-- 业务问题 2：计算每个城市每天的累计降水量，并找出降水最多的日期。
-- 考察：日期处理、GROUP BY
--
-- 关键点：
--   1. observed_at 存的是 UTC，如果直接 ::date 会按 UTC 切天，与当地日历差 8 小时。
--      必须先 AT TIME ZONE c.timezone 转成当地时间再取日期。
--   2. 只有 valid / suspect 且有降水数值的记录参与求和；missing / invalid 不参与。
--   3. 某天如果全部缺测，结果里不会出现这一天（没有行可求和）。
-- ============================================================

-- ------------------------------------------------------------
-- 2a. 每城每日累计降水量
-- ------------------------------------------------------------
SELECT
    c.city_name                                        AS "城市",
    (w.observed_at AT TIME ZONE c.timezone)::date       AS "日期",
    COUNT(*)                                            AS "有效观测数",
    COUNT(*) FILTER (WHERE w.precipitation_mm > 0)      AS "有降水的小时数",
    ROUND(SUM(w.precipitation_mm), 1)                   AS "累计降水量(mm)"
FROM cities AS c
JOIN weather_hourly AS w
    ON c.city_id = w.city_id
WHERE w.quality IN ('valid', 'suspect')
  AND w.precipitation_mm IS NOT NULL
GROUP BY
    c.city_name,
    (w.observed_at AT TIME ZONE c.timezone)::date
ORDER BY
    c.city_name,
    "日期";


-- ------------------------------------------------------------
-- 2b. 每个城市降水最多的日期
--     先在 CTE 里按「城市 + 当地日期」聚合，再在聚合结果上取每天的第一名。
--     用 RANK() 而不是 LIMIT 1：并列最湿的日期会一起保留。
-- ------------------------------------------------------------
WITH daily_precipitation AS (
    SELECT
        c.city_name                                   AS city_name,
        (w.observed_at AT TIME ZONE c.timezone)::date AS observed_date,
        ROUND(SUM(w.precipitation_mm), 1)             AS total_mm
    FROM cities AS c
    JOIN weather_hourly AS w
        ON c.city_id = w.city_id
    WHERE w.quality IN ('valid', 'suspect')
      AND w.precipitation_mm IS NOT NULL
    GROUP BY
        c.city_name,
        (w.observed_at AT TIME ZONE c.timezone)::date
),
ranked AS (
    SELECT
        city_name,
        observed_date,
        total_mm,
        RANK() OVER (
            PARTITION BY city_name
            ORDER BY total_mm DESC
        ) AS day_rank
    FROM daily_precipitation
)
SELECT
    city_name       AS "城市",
    observed_date   AS "降水最多的日期",
    total_mm        AS "累计降水量(mm)"
FROM ranked
WHERE day_rank = 1
ORDER BY total_mm DESC, city_name;
