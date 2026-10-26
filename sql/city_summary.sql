-- 业务问题：统计每个城市的平均气温、最高气温和最低气温。
SELECT 
    c.city_name AS "城市",
    ROUND(AVG(w.temperature_c)::numeric, 2) AS "平均气温(℃)",
    MAX(w.temperature_c) AS "最高气温(℃)",
    MIN(w.temperature_c) AS "最低气温(℃)"
FROM 
    cities c
JOIN 
    weather_hourly w ON c.city_id = w.city_id
WHERE 
    w.quality IN ('valid', 'suspect')
    AND w.temperature_c IS NOT NULL
GROUP BY 
    c.city_name
ORDER BY 
    "平均气温(℃)" DESC;