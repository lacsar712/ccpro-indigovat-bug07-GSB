from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        lot = DipLot(
            vat_id=pk,
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )



@router.post("/bay/vats", response_class=HTMLResponse)
async def bay_vat_create(
    request: Request,
    workshop_id: str = Form(...),
    code: str = Form(...),
    dyeType: str = Form(...),
    volumeL: str = Form(...),
    status: str = Form(Vat.STATUS_IDLE),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    try:
        wid = int(workshop_id)
    except ValueError:
        wid = -1
    ws = wid if wid > 0 else None
    new_code = code.strip()
    new_dye = dyeType.strip()
    try:
        new_volume = Decimal(volumeL)
    except InvalidOperation:
        new_volume = None
    if not db.get(Workshop, wid):
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, None, "开新缸失败：请选择有效工坊。"),
            status_code=400,
        )
    if not new_code:
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, None, "开新缸失败：缸号不能为空。"),
            status_code=400,
        )
    if not new_dye or new_volume is None or new_volume <= 0:
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, None, "开新缸失败：染种与正数容积必填。"),
            status_code=400,
        )
    new_status = status if status in STATUS_LABELS else Vat.STATUS_IDLE
    # 先查重给友好提示；唯一约束再兜住两人同时开同号的并发
    clash = (
        db.query(Vat)
        .filter(Vat.workshop_id == wid, Vat.code == new_code)
        .first()
    )
    if clash:
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, clash.id, f"缸号 {new_code} 已存在，不能重复开缸。"),
            status_code=400,
        )
    vat = Vat(
        workshop_id=wid,
        code=new_code,
        dyeType=new_dye,
        volumeL=new_volume,
        status=new_status,
    )
    db.add(vat)
    try:
        db.commit()
    except IntegrityError:
        # 并发：另一笔已抢先占用同坊同号，本笔拒绝且不入库
        db.rollback()
        winner = (
            db.query(Vat)
            .filter(Vat.workshop_id == wid, Vat.code == new_code)
            .first()
        )
        return render(
            request,
            "bay.html",
            _bay_context(
                request, db, user, ws, winner.id if winner else None,
                f"缸号 {new_code} 刚被他人先登记，本笔未入库。",
            ),
            status_code=409,
        )
    db.refresh(vat)
    return RedirectResponse(f"/?vat={vat.id}&workshop={wid}", status_code=303)


@router.post("/bay/vats/{pk}/code", response_class=HTMLResponse)
async def bay_vat_code(
    pk: int,
    request: Request,
    code: str = Form(...),
    dyeType: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    new_code = code.strip()
    if not new_code:
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, pk, "改缸号失败：缸号不能为空。"),
            status_code=400,
        )
    # 撞号一律拒绝，绝不覆盖旧缸资料；同号即自身则视为无改动
    other = (
        db.query(Vat)
        .filter(Vat.workshop_id == item.workshop_id, Vat.code == new_code, Vat.id != pk)
        .first()
    )
    if other:
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, pk, f"缸号 {new_code} 已被占用，旧缸资料保持不变。"),
            status_code=400,
        )
    item.code = new_code
    if dyeType.strip():
        item.dyeType = dyeType.strip()
    try:
        db.commit()
    except IntegrityError:
        # 并发：改号期间同坊同号被他人抢先占用，回滚以保持本缸原样
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, pk, f"缸号 {new_code} 刚被他人占用，本缸资料保持不变。"),
            status_code=409,
        )
    return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)


@router.post("/bay/workshops/{wid}/delete", response_class=HTMLResponse)
async def bay_workshop_delete(
    wid: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    shop = db.get(Workshop, wid)
    if not shop:
        return RedirectResponse("/", status_code=303)
    # 有缸的坊必须先清空染缸；拒绝硬删，避免缸被级联删除后浸染成孤儿
    vat_count = db.query(Vat).filter(Vat.workshop_id == wid).count()
    if vat_count:
        return render(
            request,
            "bay.html",
            _bay_context(
                request, db, user, wid, None,
                f"工坊「{shop.name}」仍有 {vat_count} 口染缸，不能删除；请先清空缸位。",
            ),
            status_code=400,
        )
    try:
        db.delete(shop)
        db.commit()
    except Exception as exc:
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, None, None, f"删除工坊失败：{exc}"),
            status_code=400,
        )
    return RedirectResponse("/", status_code=303)


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
