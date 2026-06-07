from fastapi import FastAPI, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware
from src.models import init_db, get_db
from src.api.routes import transactions, analytics, sync, settings, reconciliation, projects, retirement, payslips, imports, recurring
from src.config import settings as app_settings
from src.ingestion.import_service import migrate_import_artifact_paths
from src.ingestion.backfill_schema import run_backfill
from src.plugins.loader import load_plugins
from src.services.auto_tasks import start_auto_tasks, stop_auto_tasks
from src.privacy_mask import PRIVACY_MASK_ENABLED, mask_json_body
from pathlib import Path

app = FastAPI(title="Finance App API")

if PRIVACY_MASK_ENABLED:
    @app.middleware("http")
    async def privacy_mask_middleware(request, call_next):
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            return response
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        masked_body = mask_json_body(body, seed=request.url.path)
        return Response(content=masked_body, status_code=response.status_code, headers=dict(response.headers), media_type="application/json")

_PLUGIN_ICONS_DIR = Path(__file__).resolve().parents[2] / "plugins" / "icons"
if _PLUGIN_ICONS_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles
    app.mount("/api/plugin-icons", StaticFiles(directory=str(_PLUGIN_ICONS_DIR)), name="plugin-icons")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(transactions.router, prefix="/api/transactions", tags=["transactions"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])
app.include_router(sync.router, prefix="/api/sync", tags=["sync"])
app.include_router(imports.router, prefix="/api/imports", tags=["imports"])
app.include_router(recurring.router, prefix="/api/recurring", tags=["recurring"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(reconciliation.router, prefix="/api/reconciliation", tags=["reconciliation"])
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(retirement.router, prefix="/api/retirement", tags=["retirement"])
app.include_router(payslips.router, prefix="/api/payslips", tags=["payslips"])


@app.on_event("startup")
def startup():
    load_plugins()
    init_db()
    from src.models import SessionLocal
    from src.services.exchange_rates import seed_identity_rates

    if app_settings.is_demo:
        from src.demo import seed_demo_data

        db = SessionLocal()
        try:
            seed_demo_data(db)
        finally:
            db.close()
        return

    db = SessionLocal()
    try:
        seed_identity_rates(db)
        migrate_import_artifact_paths(db)
    finally:
        db.close()

    start_auto_tasks()


@app.on_event("shutdown")
def shutdown():
    stop_auto_tasks()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/meta")
def meta():
    return {"mode": app_settings.app.mode, "database_path": app_settings.database.path}


@app.get("/")
def root_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if code and state:
        return settings.complete_google_drive_connect(code=code, state=state, db=db)
    return {"status": "ok", "service": "finance-app-api"}
