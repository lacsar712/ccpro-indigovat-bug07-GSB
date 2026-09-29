import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.db import Base, SessionLocal, engine
from app.routers import auth, pages
from app.seed import ensure_seed_data


def _ensure_restrict_fk(db) -> None:
    """老库 vats→workshops 外键若仍是 CASCADE，幂等换成 RESTRICT。

    仅靠「删前数缸」挡不住与并发新建之间的竞速；RESTRICT 让数据库在
    有缸（含刚插入的缸）时直接拒绝删坊。
    """
    row = db.execute(
        text(
            "SELECT confdeltype FROM pg_constraint "
            "WHERE conname = 'vats_workshop_id_fkey' "
            "AND conrelid = 'vats'::regclass"
        )
    ).first()
    if row is not None and row[0] == "c":  # c = ON DELETE CASCADE
        db.execute(text("ALTER TABLE vats DROP CONSTRAINT vats_workshop_id_fkey"))
        db.execute(
            text(
                "ALTER TABLE vats ADD CONSTRAINT vats_workshop_id_fkey "
                "FOREIGN KEY (workshop_id) REFERENCES workshops(id) ON DELETE RESTRICT"
            )
        )
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        _ensure_restrict_fk(db)
        ensure_seed_data(db)
    finally:
        db.close()
    yield


app = FastAPI(title="IndigoVat 染缸还原台", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-indigovat-session-secret"),
    session_cookie="indigovat_session",
    same_site="lax",
    https_only=False,
)

static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(pages.router)
