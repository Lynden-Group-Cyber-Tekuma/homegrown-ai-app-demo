"""Route registry: import every route module and expose its router."""
from . import (
    activity, admin_stats, admin_tenants, admin_users, app_settings, auth,
    chat, email, guest, html, provider_keys, sanitize, scenarios, sessions,
    system, uploads, users_me,
)

all_routers = [
    html.router, auth.router, system.router, users_me.router,
    admin_users.router, admin_tenants.router, admin_stats.router,
    sessions.router, uploads.router, chat.router, sanitize.router,
    activity.router, app_settings.router, provider_keys.router,
    email.router, guest.router, scenarios.router,
]


