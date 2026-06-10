from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from .formatting import to_decimal
from .models import ReportData, TokenFlow


SECTION_IN = ("今日转入", "今日轉入", "今日轉入", "今日入金", "转入", "轉入")
SECTION_OUT = ("今日转出", "今日轉出", "今日轉出", "今日出金", "转出", "轉出")
CURRENCY_RE = re.compile(r"US\$\s*[-+]?\d[\d,]*(?:\.\d+)?", re.I)
NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9]{0,15}$")
ROW_INLINE_RE = re.compile(
    r"^\s*([A-Z][A-Z0-9]{0,15})\s+([-+]?\d[\d,]*(?:\.\d+)?)\s+(US\$\s*[-+]?\d[\d,]*(?:\.\d+)?)\s*$",
    re.I,
)
HEADER_WORDS = {"币种", "幣種", "数量", "數量", "美金计价", "美金計價", "今日转入", "今日转出"}


class ParseError(RuntimeError):
    pass


def parse_report_payload(payload: dict[str, Any]) -> ReportData:
    text = str(payload.get("text") or "")
    if not text.strip():
        text = "\n".join(_tables_to_text(payload.get("tables") or []))
    report = parse_report_text(
        text,
        captured_at=datetime.now().astimezone(),
        source_url=str(payload.get("url") or ""),
        page_title=str(payload.get("title") or ""),
    )
    return report


def parse_report_text(
    text: str,
    captured_at: datetime | None = None,
    source_url: str = "",
    page_title: str = "",
) -> ReportData:
    lines = _clean_lines(text)
    if not lines:
        raise ParseError("頁面內容為空，無法解析報表。")

    in_idx = _find_section(lines, SECTION_IN)
    out_idx = _find_section(lines, SECTION_OUT)
    if in_idx is None or out_idx is None:
        raise ParseError("找不到「今日转入 / 今日转出」區塊，請確認 Chrome 頁面已登入並停在流動性頁面。")

    total_in = _first_currency(lines[in_idx : min(in_idx + 8, len(lines))])
    total_out = _first_currency(lines[out_idx : min(out_idx + 8, len(lines))])
    if total_in is None or total_out is None:
        raise ParseError("找不到今日轉入或今日轉出的總美金數字。")

    if in_idx < out_idx:
        in_block = lines[in_idx + 1 : out_idx]
        out_block = lines[out_idx + 1 :]
    else:
        out_block = lines[out_idx + 1 : in_idx]
        in_block = lines[in_idx + 1 :]

    in_rows = _parse_token_rows(in_block)
    out_rows = _parse_token_rows(out_block)
    symbols = _ordered_symbols(in_rows, out_rows)
    if not symbols:
        raise ParseError("沒有解析到任何幣種明細。")

    flows = []
    for symbol in symbols:
        in_qty, in_usd = in_rows.get(symbol, (Decimal("0"), Decimal("0")))
        out_qty, out_usd = out_rows.get(symbol, (Decimal("0"), Decimal("0")))
        flows.append(TokenFlow(symbol=symbol, in_qty=in_qty, in_usd=in_usd, out_qty=out_qty, out_usd=out_usd))

    return ReportData(
        captured_at=captured_at or datetime.now().astimezone(),
        total_in_usd=total_in,
        total_out_usd=total_out,
        tokens=tuple(flows),
        source_url=source_url,
        page_title=page_title,
    )


def _tables_to_text(tables: list[Any]) -> list[str]:
    chunks: list[str] = []
    for table in tables:
        if not isinstance(table, list):
            continue
        for row in table:
            if isinstance(row, list):
                chunks.append("\t".join(str(cell) for cell in row if str(cell).strip()))
    return chunks


def _clean_lines(text: str) -> list[str]:
    normalized = text.replace("\r", "\n").replace("\t", "\n")
    normalized = re.sub(r"[ \u00a0]+", " ", normalized)
    return [line.strip() for line in normalized.split("\n") if line.strip()]


def _find_section(lines: list[str], aliases: tuple[str, ...]) -> int | None:
    for idx, line in enumerate(lines):
        compact = line.replace(" ", "")
        for alias in aliases:
            if alias.replace(" ", "") in compact:
                return idx
    return None


def _first_currency(lines: list[str]) -> Decimal | None:
    for line in lines:
        match = CURRENCY_RE.search(line)
        if match:
            return to_decimal(match.group(0))
    return None


def _parse_token_rows(lines: list[str]) -> dict[str, tuple[Decimal, Decimal]]:
    rows: dict[str, tuple[Decimal, Decimal]] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        inline = ROW_INLINE_RE.match(line)
        if inline:
            rows[inline.group(1).upper()] = (to_decimal(inline.group(2)), to_decimal(inline.group(3)))
            i += 1
            continue

        symbol = _extract_symbol(line)
        if not symbol:
            i += 1
            continue

        qty: Decimal | None = None
        usd: Decimal | None = None
        end = i
        for j in range(i + 1, min(i + 7, len(lines))):
            probe = lines[j]
            if _extract_symbol(probe) and (qty is not None or usd is not None):
                break
            if usd is None:
                currency = CURRENCY_RE.search(probe)
                if currency:
                    usd = to_decimal(currency.group(0))
            if qty is None:
                probe_without_currency = CURRENCY_RE.sub("", probe)
                num = NUMBER_RE.search(probe_without_currency)
                if num:
                    qty = to_decimal(num.group(0))
            end = j
            if qty is not None and usd is not None:
                break

        if qty is not None and usd is not None:
            rows[symbol] = (qty, usd)
            i = max(end + 1, i + 1)
        else:
            i += 1
    return rows


def _extract_symbol(line: str) -> str | None:
    cleaned = line.strip().upper()
    if cleaned in HEADER_WORDS:
        return None
    if "US$" in cleaned or NUMBER_RE.search(cleaned):
        return None
    if SYMBOL_RE.match(cleaned):
        return cleaned
    return None


def _ordered_symbols(
    in_rows: dict[str, tuple[Decimal, Decimal]],
    out_rows: dict[str, tuple[Decimal, Decimal]],
) -> list[str]:
    preferred = ["A", "BNB", "DAI", "ETH", "LGNS", "POL", "SLGNS", "USDC", "USDT"]
    seen = set()
    ordered = []
    for symbol in preferred + list(in_rows) + list(out_rows):
        if symbol in seen:
            continue
        if symbol in in_rows or symbol in out_rows:
            ordered.append(symbol)
            seen.add(symbol)
    return ordered
