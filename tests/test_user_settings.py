from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from auto_report.user_settings import (
    UserSettings,
    load_user_settings,
    render_message_template,
    render_telegram_filename,
    save_user_settings,
    validate_user_settings,
)


class UserSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        hkt = timezone(timedelta(hours=8))
        self.report = SimpleNamespace(
            captured_at=datetime(2026, 6, 10, 23, 30, tzinfo=hkt),
            status="净流入",
            total_in_usd=Decimal("580860.03"),
            total_out_usd=Decimal("561225.66"),
            net_usd=Decimal("19634.37"),
        )

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            saved = save_user_settings(
                state_dir,
                {
                    "telegram_filename_template": "日報_{date}_{status}",
                    "telegram_document_caption": "淨額 {net}",
                    "send_preview_image": False,
                    "otp_reminder_interval_seconds": 300,
                },
            )
            self.assertEqual(saved, load_user_settings(state_dir))
            self.assertFalse(saved.send_preview_image)
            self.assertEqual(saved.otp_reminder_interval_seconds, 300)

    def test_render_filename_and_caption(self) -> None:
        filename = render_telegram_filename("日报/{date}_{status}", self.report)
        caption = render_message_template("转入 {total_in}，净额 {net}", self.report)
        self.assertEqual(filename, "日报_2026-06-10_净流入.xlsx")
        self.assertEqual(caption, "转入 580,860.03，净额 19,634.37")

    def test_defaults_and_validation(self) -> None:
        self.assertEqual(load_user_settings(Path("/path/that/does/not/exist")), UserSettings())
        with self.assertRaises(ValueError):
            validate_user_settings(
                {
                    "telegram_filename_template": "report.xlsx",
                    "telegram_document_caption": "",
                    "send_preview_image": True,
                    "otp_reminder_interval_seconds": 10,
                }
            )

    def test_rejects_unknown_template_and_injection_text(self) -> None:
        base = {
            "telegram_filename_template": "report_{date}.xlsx",
            "telegram_document_caption": "Daily report",
            "send_preview_image": True,
            "otp_reminder_interval_seconds": 120,
        }
        for key, value in (
            ("telegram_filename_template", "../../report.xlsx"),
            ("telegram_filename_template", "report_{password}.xlsx"),
            ("telegram_document_caption", "<script>alert(1)</script>"),
            ("telegram_document_caption", "' OR 1=1 --"),
            ("telegram_document_caption", "UNION SELECT token FROM users"),
        ):
            payload = dict(base)
            payload[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    validate_user_settings(payload)


if __name__ == "__main__":
    unittest.main()
