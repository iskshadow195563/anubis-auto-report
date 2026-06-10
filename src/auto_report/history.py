from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .models import ReportData, TokenFlow


def load_history(path: Path) -> list[ReportData]:
    try:
        raw_items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw_items, list):
        return []

    reports: list[ReportData] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            reports.append(_report_from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    return _sort_newest_first(reports)


def merge_history(current: ReportData, existing: Iterable[ReportData]) -> list[ReportData]:
    by_day: dict[str, ReportData] = {}
    for report in existing:
        by_day[_history_key(report)] = report
    by_day[_history_key(current)] = current
    return _sort_newest_first(by_day.values())


def save_history(path: Path, reports: Iterable[ReportData]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [_report_to_dict(report) for report in _sort_newest_first(reports)]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sort_newest_first(reports: Iterable[ReportData]) -> list[ReportData]:
    return sorted(reports, key=lambda report: report.captured_at, reverse=True)


def _history_key(report: ReportData) -> str:
    return report.captured_at.strftime("%Y-%m-%d")


def _report_to_dict(report: ReportData) -> dict:
    return {
        "captured_at": report.captured_at.isoformat(),
        "total_in_usd": str(report.total_in_usd),
        "total_out_usd": str(report.total_out_usd),
        "source_url": report.source_url,
        "page_title": report.page_title,
        "tokens": [
            {
                "symbol": token.symbol,
                "in_qty": str(token.in_qty),
                "in_usd": str(token.in_usd),
                "out_qty": str(token.out_qty),
                "out_usd": str(token.out_usd),
            }
            for token in report.tokens
        ],
    }


def _report_from_dict(item: dict) -> ReportData:
    return ReportData(
        captured_at=datetime.fromisoformat(str(item["captured_at"])),
        total_in_usd=Decimal(str(item["total_in_usd"])),
        total_out_usd=Decimal(str(item["total_out_usd"])),
        source_url=str(item.get("source_url") or ""),
        page_title=str(item.get("page_title") or ""),
        tokens=tuple(
            TokenFlow(
                symbol=str(token["symbol"]),
                in_qty=Decimal(str(token.get("in_qty", "0"))),
                in_usd=Decimal(str(token.get("in_usd", "0"))),
                out_qty=Decimal(str(token.get("out_qty", "0"))),
                out_usd=Decimal(str(token.get("out_usd", "0"))),
            )
            for token in item.get("tokens", [])
            if isinstance(token, dict) and token.get("symbol")
        ),
    )
