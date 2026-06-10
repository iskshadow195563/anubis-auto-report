from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


APP_NAME = "Anubis Auto Report"


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        values[key] = value
    return values


def _first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.exists():
            return path
    return None


def discover_home() -> Path:
    explicit = os.environ.get("AUTO_REPORT_HOME")
    if explicit:
        return Path(explicit).expanduser().resolve()

    candidates: list[Path] = []
    cwd = Path.cwd()
    candidates.append(cwd)
    try:
        candidates.append(Path(__file__).resolve().parents[2])
    except IndexError:
        pass
    candidates.append(Path.home() / "AnubisAutoReport")
    candidates.append(Path.home() / "Documents" / "auto report")
    candidates.append(Path(sys.executable).resolve().parent)
    candidates.append(Path(sys.executable).resolve().parent.parent)

    env_path = _first_existing(path / ".env" for path in candidates)
    if env_path:
        return env_path.parent.resolve()
    return cwd.resolve()


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class AppConfig:
    home: Path
    env_path: Path | None
    admin_url: str
    admin_domain: str
    admin_username: str
    admin_password: str
    telegram_bot_token: str
    telegram_chat_id: str
    telegram_copy_chat_ids: list[str]
    telegram_allowed_chat_ids: list[str]
    telegram_allowed_usernames: list[str]
    report_interval_seconds: int
    refresh_interval_seconds: int
    telegram_poll_seconds: int
    daily_report_time: str
    otp_timeout_seconds: int
    send_xlsx: bool
    send_copy_xlsx: bool
    chrome_poll_seconds: float
    output_dir: Path
    log_dir: Path
    state_dir: Path


def load_config() -> AppConfig:
    home = discover_home()
    env_path = home / ".env"
    file_values = _parse_env_file(env_path)

    def get(key: str, default: str = "") -> str:
        return os.environ.get(key, file_values.get(key, default))

    admin_url = get("ADMIN_URL", "")
    admin_domain = get("ADMIN_DOMAIN", "")
    chat_id = get("TELEGRAM_CHAT_ID", "")
    copy_ids = _split_csv(get("TELEGRAM_COPY_CHAT_IDS", ""))
    allowed_chat_ids = _split_csv(get("TELEGRAM_ALLOWED_CHAT_IDS", chat_id))
    allowed_usernames = _split_csv(get("TELEGRAM_ALLOWED_USERNAMES", ""))

    return AppConfig(
        home=home,
        env_path=env_path if env_path.exists() else None,
        admin_url=admin_url,
        admin_domain=admin_domain,
        admin_username=get("ADMIN_USERNAME", ""),
        admin_password=get("ADMIN_PASSWORD", ""),
        telegram_bot_token=get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=chat_id,
        telegram_copy_chat_ids=copy_ids,
        telegram_allowed_chat_ids=allowed_chat_ids,
        telegram_allowed_usernames=allowed_usernames,
        report_interval_seconds=max(30, _int(get("REPORT_INTERVAL_SECONDS", "3600"), 3600)),
        refresh_interval_seconds=max(30, _int(get("REFRESH_INTERVAL_SECONDS", "60"), 60)),
        telegram_poll_seconds=max(1, _int(get("TELEGRAM_POLL_SECONDS", "5"), 5)),
        daily_report_time=get("DAILY_REPORT_TIME", "23:30"),
        otp_timeout_seconds=max(30, _int(get("OTP_TIMEOUT_SECONDS", "300"), 300)),
        send_xlsx=_bool(get("SEND_XLSX", "true"), True),
        send_copy_xlsx=_bool(get("SEND_COPY_XLSX", "true"), True),
        chrome_poll_seconds=max(0.5, float(get("CHROME_POLL_SECONDS", "2") or 2)),
        output_dir=home / "outputs",
        log_dir=home / "logs",
        state_dir=home / "state",
    )
