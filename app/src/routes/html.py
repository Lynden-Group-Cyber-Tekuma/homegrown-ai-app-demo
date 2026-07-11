"""Static HTML page routes."""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# ── HTML routes ───────────────────────────────────────────────────────────────
@router.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(open("static/index.html").read())

@router.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(open("static/login.html").read())

@router.get("/admin", response_class=HTMLResponse)
async def admin_page():
    """Serve admin UI. No server-side gate — auth is enforced by the JS overlay
    and by require_admin on every admin API endpoint."""
    return HTMLResponse(open("static/admin.html").read())
