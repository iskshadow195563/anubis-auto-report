from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


ZERO = Decimal("0")


@dataclass(frozen=True)
class TokenFlow:
    symbol: str
    in_qty: Decimal = ZERO
    in_usd: Decimal = ZERO
    out_qty: Decimal = ZERO
    out_usd: Decimal = ZERO

    @property
    def net_usd(self) -> Decimal:
        return self.in_usd - self.out_usd

    @property
    def net_qty(self) -> Decimal:
        return self.in_qty - self.out_qty

    @property
    def status(self) -> str:
        if self.net_usd > 0:
            return "净流入"
        if self.net_usd < 0:
            return "净流出"
        return "持平"


@dataclass(frozen=True)
class ReportData:
    captured_at: datetime
    total_in_usd: Decimal
    total_out_usd: Decimal
    tokens: tuple[TokenFlow, ...]
    source_url: str = ""
    page_title: str = ""

    @property
    def net_usd(self) -> Decimal:
        return self.total_in_usd - self.total_out_usd

    @property
    def status(self) -> str:
        if self.net_usd > 0:
            return "净流入"
        if self.net_usd < 0:
            return "净流出"
        return "持平"
