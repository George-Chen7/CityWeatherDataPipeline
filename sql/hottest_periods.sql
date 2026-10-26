-- ============================================================
-- 业务问题 3：找出每个城市气温最高的 5 个观测时段。
-- 考察：ORDER BY、LIMIT
--
-- 关键点：
--   直接写 ORDER BY temperature_c DESC LIMIT 5 只会得到「三城混在一起」的 5 行。
--   要「每城各 5 行」，必须让 LIMIT 对每个城市分别生效：
--   这里用 CROSS JOIN LATERAL 把子查询按城市执行一次，每次只取前 5 行。
-- ============================================================

SELECT
    c.city_name                          AS "城市",
    top5."排名",
    top5."观测时刻(当地)",
    top5."气温(℃)"
FROM cities AS c
CROSS JOIN LATERAL (
    SELECT
        ROW_NUMBER() OVER (
            ORDER BY w.temperature_c DESC, w.observed_at
        )                                                       AS "排名",
        to_char(w.observed_at AT TIME ZONE c.timezone,
                'YYYY-MM-DD HH24:MI')                           AS "观测时刻(当地)",
        w.temperature_c                                         AS "气温(℃)"
    FROM weather_hourly AS w
    WHERE w.city_id = c.city_id
      AND w.quality IN ('valid', 'suspect')
      AND w.temperature_c IS NOT NULL
    ORDER BY w.temperature_c DESC, w.observed_at
    LIMIT 5
) AS top5
ORDER BY c.city_name, top5."排名";


-- ------------------------------------------------------------
-- 等价写法（不用 LATERAL，只靠窗口函数）：
-- 窗口函数在 LIMIT 之前计算，所以可以先排名、再在外层筛 rn <= 5。
--
-- WITH ranked AS (
--     SELECT
--         c.city_name,
--         w.observed_at,
--         w.temperature_c,
--         ROW_NUMBER() OVER (
--             PARTITION BY w.city_id
--             ORDER BY w.temperature_c DESC, w.observed_at
--         ) AS rn
--     FROM cities AS c
--     JOIN weather_hourly AS w ON c.city_id = w.city_id
--     WHERE w.quality IN ('valid', 'suspect')
--       AND w.temperature_c IS NOT NULL
-- )
-- SELECT * FROM ranked WHERE rn <= 5 ORDER BY city_name, rn;
--
-- 说明：ROW_NUMBER 会强制凑满 5 行，温度并列时按 observed_at 先后切分；
--       若希望「并列第 5 名」全部保留，把 ROW_NUMBER 换成 RANK 即可。
-- ------------------------------------------------------------
