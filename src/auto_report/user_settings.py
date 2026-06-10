from __future__ import annotations

import json
import re
import string
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any


DEFAULT_FILENAME_TEMPLATE = "币种资金流日报_{date}.xlsx"
DEFAULT_DOCUMENT_CAPTION = "无公式 Excel 抄本"
ALLOWED_TEMPLATE_FIELDS = {"date", "time", "datetime", "status", "total_in", "total_out", "net"}
FILENAME_ALLOWED_PUNCTUATION = set(" _-.()[]{}")
INJECTION_PATTERNS = (
    re.compile(r"<\s*/?\s*[a-z][^>]*>", re.I),
    re.compile(r"javascript\s*:", re.I),
    re.compile(r"\bunion\s+(?:all\s+)?select\b", re.I),
    re.compile(r"\bdrop\s+table\b", re.I),
    re.compile(r"\binsert\s+into\b", re.I),
    re.compile(r"\bdelete\s+from\b", re.I),
    re.compile(r"\bupdate\s+[a-z0-9_]+\s+set\b", re.I),
    re.compile(r"(?:'|\")\s*(?:or|and)\s+\d+\s*=\s*\d+", re.I),
    re.compile(r"(?:--|/\*|\*/|;\s*(?:select|drop|insert|delete|update)\b)", re.I),
)


@dataclass(frozen=True)
class UserSettings:
    telegram_filename_template: str = DEFAULT_FILENAME_TEMPLATE
    telegram_document_caption: str = DEFAULT_DOCUMENT_CAPTION
    send_preview_image: bool = True
    otp_reminder_interval_seconds: int = 120

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_user_settings(state_dir: Path) -> UserSettings:
    path = state_dir / "user_settings.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return UserSettings()
    try:
        return validate_user_settings(raw)
    except ValueError:
        return UserSettings()


def save_user_settings(state_dir: Path, payload: dict[str, Any]) -> UserSettings:
    settings = validate_user_settings(payload)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "user_settings.json"
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)
    return settings


def validate_user_settings(payload: dict[str, Any]) -> UserSettings:
    filename = str(payload.get("telegram_filename_template", DEFAULT_FILENAME_TEMPLATE)).strip()
    caption = str(payload.get("telegram_document_caption", DEFAULT_DOCUMENT_CAPTION)).strip()
    preview = payload.get("send_preview_image", True)

    if not filename:
        raise ValueError("Telegram 文件名不可留空。")
    if len(filename) > 160:
        raise ValueError("Telegram 文件名最多 160 個字元。")
    if len(caption) > 900:
        raise ValueError("隨文件文字最多 900 個字元。")
    if not isinstance(preview, bool):
        raise ValueError("預覽圖片設定格式不正確。")

    _validate_template(filename, "Telegram 文件名")
    _validate_template(caption, "隨文件文字")
    _validate_filename_text(filename)
    _validate_plain_text(caption, "隨文件文字", allow_newlines=True)

    interval_value = payload.get("otp_reminder_interval_seconds")
    if interval_value is None:
        try:
            interval_value = int(payload.get("otp_reminder_interval_minutes", 2)) * 60
        except (TypeError, ValueError) as exc:
            raise ValueError("驗證碼提醒頻率必須是數字。") from exc
    try:
        interval = int(interval_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("驗證碼提醒頻率必須是數字。") from exc
    if interval != 0 and not 30 <= interval <= 1800:
        raise ValueError("驗證碼提醒頻率必須為 0，或介乎 30 至 1800 秒。")

    return UserSettings(
        telegram_filename_template=filename,
        telegram_document_caption=caption,
        send_preview_image=preview,
        otp_reminder_interval_seconds=interval,
    )


def render_message_template(template: str, report) -> str:
    values = {
        "date": report.captured_at.strftime("%Y-%m-%d"),
        "time": report.captured_at.strftime("%H-%M"),
        "datetime": report.captured_at.strftime("%Y-%m-%d_%H-%M"),
        "status": report.status,
        "total_in": _decimal_text(report.total_in_usd),
        "total_out": _decimal_text(report.total_out_usd),
        "net": _decimal_text(report.net_usd),
    }
    return _safe_format(template, values)


def render_telegram_filename(template: str, report) -> str:
    filename = render_message_template(template, report).strip()
    filename = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", filename)
    filename = re.sub(r"\s+", " ", filename).strip(" .")
    if not filename:
        filename = f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    if not filename.lower().endswith(".xlsx"):
        filename += ".xlsx"
    if len(filename) > 180:
        filename = filename[:175].rstrip(" .") + ".xlsx"
    return filename


def _safe_format(template: str, values: dict[str, str]) -> str:
    try:
        return template.format_map(_TemplateValues(values))
    except (ValueError, KeyError) as exc:
        raise ValueError(f"模板格式錯誤: {exc}") from exc


def _decimal_text(value: Decimal) -> str:
    return f"{value:,.2f}"


class _TemplateValues(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _validate_template(value: str, field_name: str) -> None:
    try:
        parsed = list(string.Formatter().parse(value))
    except ValueError as exc:
        raise ValueError(f"{field_name}的模板括號格式不正確。") from exc
    for _, field_name_value, format_spec, conversion in parsed:
        if field_name_value is None:
            continue
        if field_name_value not in ALLOWED_TEMPLATE_FIELDS:
            raise ValueError(f"不支援模板變數 {{{field_name_value}}}。")
        if format_spec or conversion:
            raise ValueError("模板變數不可加入格式指令或轉換符號。")


def _validate_filename_text(value: str) -> None:
    _validate_plain_text(value, "Telegram 文件名", allow_newlines=False)
    for char in value:
        if char.isalnum() or char in FILENAME_ALLOWED_PUNCTUATION:
            continue
        raise ValueError(f"Telegram 文件名含有不允許的字元: {char}")
    if ".." in value:
        raise ValueError("Telegram 文件名不可包含連續兩個句點。")
    if value.startswith("."):
        raise ValueError("Telegram 文件名不可用句點開頭。")


def _validate_plain_text(value: str, field_name: str, allow_newlines: bool) -> None:
    for char in value:
        if char in {"\n", "\r"} and allow_newlines:
            continue
        if char == "\t" and allow_newlines:
            continue
        if ord(char) < 32 or ord(char) == 127:
            raise ValueError(f"{field_name}不可包含控制字元。")
    for pattern in INJECTION_PATTERNS:
        if pattern.search(value):
            raise ValueError(f"{field_name}包含不允許的程式碼或注入語句。")
