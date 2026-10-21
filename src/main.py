"""ETL 入口：解析参数，串联 Extract -> Transform -> Load，并给出退出码。

退出码约定：
    0  全部成功
    1  配置/数据库等致命错误
    2  部分城市抽取失败，但其余城市已成功入库
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config, extract, load, transform

logger = logging.getLogger("city_weather")

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_PARTIAL = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="城市逐小时天气 ETL：Open-Meteo -> 清洗校验 -> PostgreSQL（幂等写入）",
    )
    parser.add_argument(
        "--cities",
        nargs="+",
        choices=sorted(config.CITIES),
        metavar="CITY",
        help="要处理的城市，默认全部：" + ", ".join(config.CITIES),
    )
    parser.add_argument(
        "--start", metavar="YYYY-MM-DD", help="开始日期，默认 13 天前"
    )
    parser.add_argument(
        "--end", metavar="YYYY-MM-DD", help="结束日期，默认今天"
    )
    parser.add_argument(
        "--from-raw",
        action="store_true",
        help="不请求 API，直接回放 data/raw 下同区间的原始响应",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只抽取、清洗并输出 data/processed，不连接数据库",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="日志级别，默认 INFO",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _acquire_payloads(
    city_codes: list[str], start, end, from_raw: bool
) -> tuple[list[extract.CityPayload], dict[str, str]]:
    if not from_raw:
        return extract.extract_all(city_codes, start=start, end=end)

    payloads: list[extract.CityPayload] = []
    failures: dict[str, str] = {}
    for city_code in city_codes:
        try:
            payloads.append(
                extract.load_raw_payload(city_code, start=start, end=end)
            )
        except extract.ExtractError as exc:
            failures[city_code] = str(exc)
            logger.error("城市 %s 回放失败：%s", city_code, exc)
    return payloads, failures


def run(args: argparse.Namespace) -> int:
    config.validate_cities()
    start, end = config.resolve_date_range(args.start, args.end)
    city_codes = config.resolve_cities(args.cities)

    mode = "raw 回放" if args.from_raw else "在线抓取"
    suffix = " + dry-run" if args.dry_run else ""
    logger.info(
        "ETL 开始 | 城市=%s | 区间=%s ~ %s | 模式=%s%s",
        ", ".join(city_codes),
        start,
        end,
        mode,
        suffix,
    )

    payloads, failures = _acquire_payloads(city_codes, start, end, args.from_raw)
    for city_code, reason in failures.items():
        logger.error("抽取失败 | %s | %s", city_code, reason)

    if not payloads:
        logger.error("没有任何城市取得数据，流程终止")
        return EXIT_FATAL

    result = transform.transform_all(payloads)
    report = transform.write_outputs(result)

    logger.info(
        "清洗完成 | 输入=%d | 输出=%d | 剔除=%d | 批内重复=%d",
        report["input_records"],
        report["output_records"],
        report["rejected_records"],
        report["duplicates_removed"],
    )
    logger.info(
        "质量分布 | valid=%d missing=%d suspect=%d invalid=%d",
        report["quality_counts"]["valid"],
        report["quality_counts"]["missing"],
        report["quality_counts"]["suspect"],
        report["quality_counts"]["invalid"],
    )

    if args.dry_run:
        logger.info("--dry-run：跳过数据库写入")
        return EXIT_PARTIAL if failures else EXIT_OK

    if not result.records:
        logger.error("清洗后没有可入库的记录，流程终止")
        return EXIT_FATAL

    try:
        stats = load.load_all(result.records, [p.city_code for p in payloads])
    except load.DatabaseError as exc:
        logger.error("%s", exc)
        return EXIT_FATAL

    logger.info(
        "入库完成 | 城市维度=%d | 提交=%d | 新增/更新=%d | 未变化=%d",
        stats.cities_upserted,
        stats.rows_received,
        stats.rows_affected,
        stats.rows_unchanged,
    )
    if stats.rows_affected == 0:
        logger.info("本次没有任何业务字段变化 —— 幂等命中（重复运行不会产生新记录）")

    if failures:
        logger.warning(
            "部分城市失败：%s；其余城市已正常入库",
            ", ".join(failures),
        )
        return EXIT_PARTIAL
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    try:
        return run(args)
    except config.ConfigError as exc:
        logger.error("配置错误：%s", exc)
        return EXIT_FATAL
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return EXIT_FATAL
    except Exception:  # noqa: BLE001 - 入口兜底，避免堆栈直接甩给调度器
        logger.exception("未捕获异常，流程终止")
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
