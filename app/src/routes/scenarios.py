"""Demo scenario CRUD and sync routes."""
import json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import require_admin
from database import get_db
from models import AppSetting, DemoScenario, User
from src.services.audit import _log_audit
from src.services.scenarios_seed import _SCENARIOS_FILE

router = APIRouter()

# ── Demo Scenarios ────────────────────────────────────────────────────────────

@router.get("/demo-scenarios")
async def list_demo_scenarios_public(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(DemoScenario)
        .where(DemoScenario.is_active == True)
        .order_by(DemoScenario.sort_order, DemoScenario.id)
    )
    scenarios = result.scalars().all()
    return [
        {
            "id": s.id, "key": s.key, "title": s.title, "category": s.category,
            "severity": s.severity, "prompt": s.prompt, "expected_action": s.expected_action,
            "description": s.description, "attacker_goal": s.attacker_goal,
            "why_caught": s.why_caught, "talking_point": s.talking_point,
            "entities": s.entities or [], "meta": s.meta or {},
            "sort_order": s.sort_order,
        }
        for s in scenarios
    ]


@router.get("/admin/demo-scenarios")
async def list_demo_scenarios_admin(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DemoScenario).order_by(DemoScenario.sort_order, DemoScenario.id)
    )
    scenarios = result.scalars().all()
    return [
        {
            "id": s.id, "key": s.key, "title": s.title, "category": s.category,
            "severity": s.severity, "prompt": s.prompt, "expected_action": s.expected_action,
            "description": s.description, "attacker_goal": s.attacker_goal,
            "why_caught": s.why_caught, "talking_point": s.talking_point,
            "entities": s.entities or [], "meta": s.meta or {},
            "sort_order": s.sort_order, "is_active": s.is_active,
        }
        for s in scenarios
    ]


@router.post("/admin/demo-scenarios", status_code=201)
async def create_demo_scenario(
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    key = body.get("key") or body.get("title", "").lower().replace(" ", "_")[:60]
    s = DemoScenario(
        key=key, title=body.get("title", ""), category=body.get("category", ""),
        severity=body.get("severity", "MEDIUM"), prompt=body.get("prompt", ""),
        expected_action=body.get("expected_action", "block"),
        description=body.get("description"), attacker_goal=body.get("attacker_goal"),
        why_caught=body.get("why_caught"), talking_point=body.get("talking_point"),
        entities=body.get("entities") or None, meta=body.get("meta") or None,
        sort_order=int(body.get("sort_order", 0)), is_active=body.get("is_active", True),
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    await _log_audit(db, admin.id, admin.email, "scenario_created", f"'{s.title}' · {s.category} · {s.severity}")
    return {"id": s.id, "key": s.key}


@router.patch("/admin/demo-scenarios/{scenario_id}")
async def update_demo_scenario(
    scenario_id: int,
    body: dict,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(DemoScenario, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    for field in ("title", "category", "severity", "prompt", "expected_action",
                  "description", "attacker_goal", "why_caught", "talking_point",
                  "sort_order", "is_active"):
        if field in body:
            setattr(s, field, body[field])
    if "entities" in body:
        s.entities = body["entities"] or None
    if "meta" in body:
        s.meta = body["meta"] or None
    await db.commit()
    await _log_audit(db, admin.id, admin.email, "scenario_updated", f"'{s.title}' · {s.category}")
    return {"ok": True}


@router.delete("/admin/demo-scenarios/{scenario_id}", status_code=204)
async def delete_demo_scenario(
    scenario_id: int,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(DemoScenario, scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    await _log_audit(db, admin.id, admin.email, "scenario_deleted", f"'{s.title}' · {s.category}")
    await db.delete(s)
    await db.commit()


@router.post("/admin/demo-scenarios/save-master", status_code=200)
async def save_master_scenarios(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DemoScenario).order_by(DemoScenario.sort_order, DemoScenario.id)
    )
    scenarios = result.scalars().all()
    data = [
        {k: v for k, v in {
            "key": s.key,
            "title": s.title,
            "category": s.category,
            "severity": s.severity,
            "expected_action": s.expected_action,
            "sort_order": s.sort_order,
            "description": s.description,
            "attacker_goal": s.attacker_goal,
            "why_caught": s.why_caught,
            "talking_point": s.talking_point,
            "entities": s.entities,
            "meta": s.meta,
            "prompt": s.prompt,
        }.items() if v is not None}
        for s in scenarios
    ]
    try:
        os.makedirs(os.path.dirname(_SCENARIOS_FILE), exist_ok=True)
        with open(_SCENARIOS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write scenarios.json: {e}")

    await _log_audit(db, admin.id, admin.email, "scenarios_master_saved", f"{len(data)} scenarios written to scenarios.json")
    return {"saved": len(data)}


@router.post("/admin/demo-scenarios/validate-url", status_code=200)
async def validate_scenarios_url(
    body: dict,
    admin: User = Depends(require_admin),
):
    """Fetch the given URL and confirm it is a valid scenarios JSON array."""
    from urllib.parse import urlparse
    _ALLOWED_HOST = "raw.githubusercontent.com"
    url = (body.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "Please enter a URL."}
    if not url.startswith("https://"):
        return {"ok": False, "error": "The URL doesn't look right — it should start with https://"}
    parsed = urlparse(url)
    if parsed.hostname != _ALLOWED_HOST:
        return {"ok": False, "error": f"Only URLs from {_ALLOWED_HOST} are supported."}
    # Reconstruct URL from validated components — never pass raw user input to httpx
    safe_url = f"https://{_ALLOWED_HOST}{parsed.path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(safe_url)
    except Exception:
        return {"ok": False, "error": "Couldn't reach that URL. Check the address is correct and the server is accessible."}
    if resp.status_code == 404:
        return {"ok": False, "error": "File not found (404). Check the repository name, branch, and file path are all correct."}
    if resp.status_code == 401 or resp.status_code == 403:
        return {"ok": False, "error": "Access denied. The repository may be private — use a raw URL that includes an access token."}
    if resp.status_code != 200:
        return {"ok": False, "error": f"The server returned an unexpected response ({resp.status_code}). Double-check the URL."}
    try:
        data = resp.json()
    except Exception:
        return {"ok": False, "error": "The URL loaded successfully but didn't return JSON. Make sure it points directly to the raw scenarios.json file, not a GitHub webpage."}
    if not isinstance(data, list):
        return {"ok": False, "error": "The file loaded but isn't in the right format — expected a JSON array of scenarios."}
    if len(data) == 0:
        return {"ok": False, "error": "The file is empty — no scenarios were found in it."}
    return {"ok": True, "count": len(data)}


@router.post("/admin/demo-scenarios/sync", status_code=200)
async def sync_scenarios_from_url(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Fetch scenarios.json from the configured sync URL and upsert into the DB."""
    _DEFAULT_SYNC_URL = "https://raw.githubusercontent.com/prompt-security/homegrown-ai-app-demo/main/app/data/scenarios.json"
    row = await db.get(AppSetting, "scenarios_sync_url")
    url = (row.value or "").strip() if row else ""
    if not url:
        url = _DEFAULT_SYNC_URL
    branch_row = await db.get(AppSetting, "scenarios_sync_branch")
    branch = (branch_row.value or "").strip() if branch_row else "main"
    # Substitute branch by parsing the URL properly — never use substring matching
    from urllib.parse import urlparse as _up
    _parsed_url = _up(url)
    if branch and _parsed_url.hostname == "raw.githubusercontent.com":
        # path: /{owner}/{repo}/{branch}/{rest...}
        path_parts = _parsed_url.path.split("/")  # ['', owner, repo, branch, ...]
        if len(path_parts) >= 4:
            path_parts[3] = branch
            url = f"https://raw.githubusercontent.com{'/'.join(path_parts)}"

    from urllib.parse import urlparse as _urlparse
    _sync_host = "raw.githubusercontent.com"
    _parsed = _urlparse(url)
    if _parsed.hostname != _sync_host:
        raise HTTPException(status_code=400, detail="Sync URL must point to raw.githubusercontent.com.")
    # Reconstruct URL from validated components — never pass raw user input to httpx
    _safe_url = f"https://{_sync_host}{_parsed.path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_safe_url)
            resp.raise_for_status()
            remote = resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch scenarios from URL: {exc}")

    if not isinstance(remote, list):
        raise HTTPException(status_code=422, detail="Remote file is not a JSON array.")

    # Build set of keys from remote
    remote_keys = set()
    for item in remote:
        if isinstance(item, dict) and item.get("title"):
            remote_keys.add(item.get("key") or item["title"].lower().replace(" ", "_")[:60])

    # Delete local scenarios not in remote
    all_local = (await db.execute(select(DemoScenario))).scalars().all()
    removed = 0
    for s in all_local:
        if s.key not in remote_keys:
            await db.delete(s)
            removed += 1

    # Add / update
    added = updated = 0
    for item in remote:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        key = item.get("key") or item["title"].lower().replace(" ", "_")[:60]
        existing = await db.scalar(select(DemoScenario).where(DemoScenario.key == key))
        if existing:
            for field in ("title","category","severity","expected_action","sort_order",
                          "description","attacker_goal","why_caught","talking_point",
                          "entities","meta","prompt"):
                if field in item:
                    setattr(existing, field, item[field])
            updated += 1
        else:
            db.add(DemoScenario(
                key=key,
                title=item.get("title",""),
                category=item.get("category",""),
                severity=item.get("severity","medium"),
                expected_action=item.get("expected_action",""),
                sort_order=item.get("sort_order",0),
                description=item.get("description"),
                attacker_goal=item.get("attacker_goal"),
                why_caught=item.get("why_caught"),
                talking_point=item.get("talking_point"),
                entities=item.get("entities"),
                meta=item.get("meta"),
                prompt=item.get("prompt",""),
            ))
            added += 1

    await db.commit()
    await _log_audit(db, admin.id, admin.email, "scenarios_synced",
                     f"Synced from {url}: {added} added, {updated} updated, {removed} removed")
    return {"added": added, "updated": updated, "removed": removed, "total": len(remote)}
