-- ============================================================
-- City Weather Data Pipeline - schema（与现有 Task 3 表结构一致）
-- 幂等 DDL：首次运行建表，重复运行全部跳过
-- ============================================================

CREATE TABLE IF NOT EXISTS cities (
    city_id    SMALLSERIAL PRIMARY KEY,
    city_key   VARCHAR(50)  NOT NULL UNIQUE,   -- 稳定业务键: shenzhen / hong_kong / singapore
    city_name  VARCHAR(100) NOT NULL,
    latitude   NUMERIC      NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude  NUMERIC      NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    timezone   VARCHAR(64)  NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT now()
);

COMMENT ON TABLE  cities IS '城市维度表';
COMMENT ON COLUMN cities.city_key IS '程序内使用的稳定标识，upsert 的冲突键';

CREATE TABLE IF NOT EXISTS weather_hourly (
    id               BIGSERIAL PRIMARY KEY,
    city_id          SMALLINT    NOT NULL REFERENCES cities (city_id) ON DELETE RESTRICT,
    observed_at      TIMESTAMPTZ NOT NULL,     -- 统一存 UTC 瞬时
    temperature_c    NUMERIC,
    precipitation_mm NUMERIC,
    wind_speed_kmh   NUMERIC,
    quality          VARCHAR(16) NOT NULL DEFAULT 'valid'
                     CHECK (quality IN ('valid', 'missing', 'suspect', 'invalid')),
    issues           JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 幂等的核心：同一城市同一观测时刻只允许一行
    CONSTRAINT uq_weather_hourly_city_time UNIQUE (city_id, observed_at),

    -- 物理下限只在记录被判为 invalid 时放行，便于保留问题数据排查
    CONSTRAINT ck_precipitation_nonneg
        CHECK (quality = 'invalid' OR precipitation_mm IS NULL OR precipitation_mm >= 0),
    CONSTRAINT ck_wind_speed_nonneg
        CHECK (quality = 'invalid' OR wind_speed_kmh IS NULL OR wind_speed_kmh >= 0),
    CONSTRAINT ck_temperature_physical
        CHECK (quality = 'invalid' OR temperature_c IS NULL
               OR (temperature_c >= -100 AND temperature_c <= 100))
);

COMMENT ON TABLE  weather_hourly IS '逐小时天气事实表，observed_at 为 UTC';
COMMENT ON COLUMN weather_hourly.observed_at IS '观测时刻（UTC），与 city_id 组成唯一键';
COMMENT ON COLUMN weather_hourly.quality IS 'valid / missing / suspect / invalid 四态标签';
COMMENT ON COLUMN weather_hourly.issues IS '命中的质量规则列表，便于回溯';

CREATE INDEX IF NOT EXISTS idx_weather_hourly_observed_at
    ON weather_hourly (observed_at);
CREATE INDEX IF NOT EXISTS idx_weather_hourly_quality
    ON weather_hourly (quality);
