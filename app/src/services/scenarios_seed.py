"""Demo scenario seeding from the bundled scenarios.json file."""
import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import DemoScenario

logger = logging.getLogger(__name__)

_SCENARIOS_FILE = str(Path(__file__).resolve().parents[2] / "data" / "scenarios.json")

def _load_scenarios_file() -> list[dict]:
    try:
        with open(_SCENARIOS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("scenarios.json not found at %s — no scenarios will be seeded", _SCENARIOS_FILE)
        return []
    except Exception as e:
        logger.error("Failed to load scenarios.json: %s", e)
        return []


async def _seed_demo_scenarios(db: AsyncSession) -> None:
    count = await db.scalar(select(func.count()).select_from(DemoScenario))
    if count and count > 0:
        return
    scenarios = _load_scenarios_file()
    for d in scenarios:
        db.add(DemoScenario(
            key=d["key"], title=d["title"], category=d["category"],
            severity=d["severity"], prompt=d["prompt"],
            expected_action=d["expected_action"],
            description=d.get("description"),
            attacker_goal=d.get("attacker_goal"),
            why_caught=d.get("why_caught"),
            talking_point=d.get("talking_point"),
            entities=d.get("entities"),
            meta=d.get("meta"),
            sort_order=d.get("sort_order", 0),
        ))
    await db.commit()
    logger.info("Demo scenarios seeded from scenarios.json (%d rows)", len(scenarios))
