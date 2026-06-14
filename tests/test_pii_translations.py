"""
Real PS API integration tests for translated demo scenarios.

Requires env vars:
  PS_BASE_URL  — e.g. https://your-tenant.promptsecurity.ai
  PS_APP_ID    — APP-ID header value

Without these vars, all tests skip (CI shows yellow, not red).

Coverage guard: any scenario in scenarios.json that has a meta.prompt_XX key must
appear in the parametrize tables below — pytest.fail() if not.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Ensure app/ is on the path so we can import PromptSecurityClient
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from prompt_security import PromptSecurityClient  # noqa: E402

SCENARIOS_PATH = Path(__file__).parent.parent / "app" / "data" / "scenarios.json"

# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def ps_client():
    base = os.environ.get("PS_BASE_URL", "").rstrip("/")
    app_id = os.environ.get("PS_APP_ID", "")
    if not base or not app_id:
        pytest.skip("PS_BASE_URL / PS_APP_ID not set — skipping PS API tests")
    return PromptSecurityClient(base_url=base, app_id=app_id)


@pytest.fixture(scope="session")
def scenarios():
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {s["key"]: s for s in data}


# ── Coverage guard ────────────────────────────────────────────────────────────

def _registered_translations():
    """(key, lang) pairs that have a test in this file."""
    return {
        ("pii_JP", "ja"),
        ("pii_DE", "de"),
        ("pii_IN", "hi"),
        ("pii_IL", "he"),
        ("pii_SG", "zh"),
        ("pii_BR", "pt"),
        ("pii_MY", "ms"),
        ("injection", "ja"),
        ("injSoft", "ja"),
    }


def test_translation_coverage():
    """Fail if any meta.prompt_XX in scenarios.json lacks a corresponding test."""
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        scenarios = json.load(f)

    registered = _registered_translations()
    missing = []
    for s in scenarios:
        for k in s.get("meta", {}):
            m = re.match(r"^prompt_([a-z]+)$", k)
            if m and m.group(1) != "en":
                pair = (s["key"], m.group(1))
                if pair not in registered:
                    missing.append(pair)

    if missing:
        pytest.fail(
            "These translations in scenarios.json have no PS API test:\n"
            + "\n".join(f"  {key!r} / {lang!r}" for key, lang in missing)
            + "\nAdd them to test_pii_translations.py and _registered_translations()."
        )


# ── PII tests — assert action == "modify" ────────────────────────────────────

@pytest.mark.parametrize("key,lang,expected_entities", [
    (
        "pii_JP", "ja",
        ["JAPAN_MY_NUMBER_PERSONAL", "JAPAN_SOCIAL_INSURANCE_NUMBER_SIN"],
    ),
    (
        "pii_DE", "de",
        ["GERMANY_ID_NUMBER", "GERMANY_PASSPORT_NUMBER"],
    ),
    (
        "pii_IN", "hi",
        ["INDIA_AADHAAR_NUMBER", "INDIA_PAN_NUMBER"],
    ),
    (
        "pii_IL", "he",
        ["IL_ID_NUMBER"],
    ),
    (
        "pii_SG", "zh",
        ["SG_NRIC_FIN"],
    ),
    (
        "pii_BR", "pt",
        ["BR_CPF_NUMBER"],
    ),
    (
        "pii_MY", "ms",
        ["MALAYSIA_ID_NUMBER"],
    ),
])
@pytest.mark.asyncio
async def test_translated_pii_triggers_modify(ps_client, scenarios, key, lang, expected_entities):
    s = scenarios[key]
    prompt = s["meta"][f"prompt_{lang}"]
    result = await ps_client.protect_prompt(prompt)
    assert result.action == "modify", (
        f"{key}/{lang}: expected action=modify, got {result.action!r}"
    )
    detected = set()
    for v in result.violations or []:
        if isinstance(v, dict):
            detected.add(v.get("type") or v.get("entity_type") or "")
        elif isinstance(v, str):
            detected.add(v)
    detected.discard("")
    for entity in expected_entities:
        assert entity in detected, (
            f"{key}/{lang}: expected entity {entity!r} not in violations {detected}"
        )


# ── Injection tests — assert action == "block" ───────────────────────────────

@pytest.mark.parametrize("key,lang", [
    ("injection", "ja"),
    ("injSoft", "ja"),
])
@pytest.mark.asyncio
async def test_translated_injection_triggers_block(ps_client, scenarios, key, lang):
    s = scenarios[key]
    prompt = s["meta"][f"prompt_{lang}"]
    result = await ps_client.protect_prompt(prompt)
    assert result.action == "block", (
        f"{key}/{lang}: expected action=block, got {result.action!r}"
    )
