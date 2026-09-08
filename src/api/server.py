from fastapi import FastAPI, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session
from fastapi.middleware.cors import CORSMiddleware
from src.models import init_db, get_db
from src.api.routes import transactions, analytics, sync, settings, reconciliation, projects, retirement, payslips, imports, recurring
from src.config import settings as app_settings
from src.ingestion.import_service import migrate_import_artifact_paths
from src.plugins.loader import load_plugins
from src.ingestion.plaid_activity import migrate_investment_transactions
from src.services.auto_tasks import start_auto_tasks, stop_auto_tasks
from src.privacy_mask import PRIVACY_MASK_ENABLED, mask_json_body
from src.api.local_auth import install_local_token_gate
from src.api.static_web import index_file, mount_static_frontend
import os
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

def _plugin_icon_dirs() -> list[Path]:
    dirs = [Path(__file__).resolve().parents[2] / "plugins" / "icons"]
    if external_dir := os.environ.get("FINANCE_PLUGINS_DIR"):
        dirs.append(Path(external_dir) / "icons")
    return [path for path in dirs if path.is_dir()]


@app.get("/api/plugin-icons/{filename}")
def plugin_icon(filename: str):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse

    if Path(filename).name != filename:
        raise HTTPException(status_code=404)
    for directory in _plugin_icon_dirs():
        path = directory / filename
        if path.is_file():
            return FileResponse(path)
    raise HTTPException(status_code=404)

# No-op unless FINANCE_APP_LOCAL_TOKEN is set, which only the desktop shell does.
install_local_token_gate(app)

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
app.include_router(projects.contacts_router, prefix="/api/contacts", tags=["contacts"])
app.include_router(retirement.router, prefix="/api/retirement", tags=["retirement"])
app.include_router(payslips.router, prefix="/api/payslips", tags=["payslips"])


@app.on_event("startup")
def startup():
    load_plugins()
    init_db()
    from src.models import SessionLocal
    db = SessionLocal()
    try:
        migrate_investment_transactions(db)
    finally:
        db.close()
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
    # When a built frontend is bundled, "/" is the app. The OAuth callback above keeps
    # priority because it is identified by its query parameters, not by its path.
    if (index := index_file()) is not None:
        from fastapi.responses import FileResponse

        return FileResponse(index)
    return {"status": "ok", "service": "finance-app-api"}


# Last, deliberately: Starlette matches routes in registration order, so a mount at "/"
# added any earlier would shadow every API route above it.
mount_static_frontend(app)
