"""染缸状态业务规则。"""

from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models import DipLot, Vat


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸设为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def validate_vat_status_change(vat: Vat, new_status: str, latest: Optional[DipLot]) -> None:
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)


def assert_code_available(
    db: Session,
    workshop_id: int,
    code: str,
    *,
    exclude_vat_id: Optional[int] = None,
) -> None:
    """同工坊内缸号唯一；撞号抛 VatRuleError，绝不覆盖旧缸资料。

    注意：调用方必须在把新缸 add 进会话之前调用，否则 autoflush 会先撞库。
    """
    existing = (
        db.query(Vat.id)
        .filter(Vat.workshop_id == workshop_id, Vat.code == code)
    )
    if exclude_vat_id is not None:
        existing = existing.filter(Vat.id != exclude_vat_id)
    if existing.first() is not None:
        raise VatRuleError(f"缸号 {code} 在本工坊已存在，请换一个缸号。")
