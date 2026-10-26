-- ============================================================
-- 业务问题 4：按照研究期间的平均气温给三个城市排名。
-- 考察：CTE、RANK()
--
-- 思路拆成两步（CTE 让每一步都可单独 SELECT 出来检查）：
--   1. city_stats：先按城市聚合出平均气温；
--   2. ranked    ：在聚合结果上开窗排名，RANK() 按平均气温降序。
-- ============================================================

WITH city_stats AS (
    SELECT
        c.city_name             AS city_name,
        AVG(w.temperature_c)    AS avg_temp_c,
        COUNT(*)                AS observation_count
    FROM cities AS c
    JOIN weather_hourly AS w
        ON c.city_id = w.city_id
    WHERE w.quality IN ('valid', 'suspect')
      AND w.temperature_c IS NOT NULL
    GROUP BY c.city_name
),
ranked AS (
    SELECT
        city_name,
        avg_temp_c,
        observation_count,
        RANK() OVER (ORDER BY avg_temp_c DESC) AS city_rank
    FROM city_stats
)
SELECT
    city_rank           AS "排名",
    city_name           AS "城市",
    ROUND((avg_temp_c)::numeric, 2) AS "平均气温(℃)",
    observation_count   AS "参与统计的观测数"
FROM ranked
ORDER BY city_rank;

-- 说明：
--   * RANK() 遇到相同平均气温会给同名次，并跳过后面的名次（1,1,3）；
--     DENSE_RANK() 不跳号（1,1,2）；ROW_NUMBER() 则强制排出 1,2,3。
--   * 本例三个城市平均气温几乎不可能完全相同，三种写法结果一致；
--     但如果出现并列，用 RANK() 更符合「并列第几」的业务语义。
--   * 这个两步写法可以压成一层：窗口函数可以直接套在 GROUP BY 上，
--     RANK() OVER (ORDER BY AVG(w.temperature_c) DESC) 也能算，
--     拆成 CTE 只是为了可读、可逐段验证。
