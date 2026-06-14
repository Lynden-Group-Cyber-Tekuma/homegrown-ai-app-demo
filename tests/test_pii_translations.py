"""
Real PS API integration tests for translated demo scenarios.

Requires env vars:
  PS_BASE_URL  — e.g. https://your-tenant.promptsecurity.ai
  PS_APP_ID    — APP-ID header value

Without these vars, all tests skip (CI shows yellow, not red).

Coverage guard: any scenario in scenarios.json that has a meta.prompt_XX key must
appear in _registered_translations() below — pytest.fail() if not.
"""

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from prompt_security import PromptSecurityClient  # noqa: E402

SCENARIOS_PATH = Path(__file__).parent.parent / "app" / "data" / "scenarios.json"


# ── Fixtures ──────────────────────────────────────────────────────────────────

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


def _findings_entities(result) -> set:
    """Extract entity_type values from raw PS findings."""
    found = set()
    findings = result.raw.get("result", {}).get("prompt", {}).get("findings", {})
    for detections in findings.values():
        if not isinstance(detections, list):
            continue
        for d in detections:
            if isinstance(d, dict) and "entity_type" in d:
                found.add(d["entity_type"])
    return found


async def _assert_pii_modify(ps_client, scenarios, key, lang):
    s = scenarios[key]
    prompt = s["meta"][f"prompt_{lang}"] if lang != "en" else s["prompt"]
    result = await ps_client.protect_prompt(prompt)
    detected = _findings_entities(result)
    assert result.action == "modify", (
        f"{key}/{lang}: expected action=modify, got {result.action!r}. "
        f"Detected entities: {detected or '(none)'}"
    )
    return detected


# ── Coverage guard ────────────────────────────────────────────────────────────

def _registered_translations():
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


# ── Japan ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_japan_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_JP", "en")
    print(f"\npii_JP/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_japan_japanese(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_JP", "ja")
    print(f"\npii_JP/ja detected: {detected}")


# ── Germany ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_germany_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_DE", "en")
    print(f"\npii_DE/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_germany_german(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_DE", "de")
    print(f"\npii_DE/de detected: {detected}")


# ── India ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_india_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IN", "en")
    print(f"\npii_IN/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_india_hindi(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IN", "hi")
    print(f"\npii_IN/hi detected: {detected}")


# ── Israel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_israel_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IL", "en")
    print(f"\npii_IL/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_israel_hebrew(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IL", "he")
    print(f"\npii_IL/he detected: {detected}")


# ── Singapore ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_singapore_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_SG", "en")
    print(f"\npii_SG/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_singapore_mandarin(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_SG", "zh")
    print(f"\npii_SG/zh detected: {detected}")


# ── Brazil ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_brazil_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_BR", "en")
    print(f"\npii_BR/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_brazil_portuguese(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_BR", "pt")
    print(f"\npii_BR/pt detected: {detected}")


# ── Malaysia ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_malaysia_english(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_MY", "en")
    print(f"\npii_MY/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_malaysia_malay(ps_client, scenarios):
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_MY", "ms")
    print(f"\npii_MY/ms detected: {detected}")


# ── Prompt Injection ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_injection_english(ps_client, scenarios):
    s = scenarios["injection"]
    result = await ps_client.protect_prompt(s["prompt"])
    assert result.action == "block", (
        f"injection/en: expected block, got {result.action!r}"
    )


@pytest.mark.asyncio
async def test_injection_japanese(ps_client, scenarios):
    s = scenarios["injection"]
    result = await ps_client.protect_prompt(s["meta"]["prompt_ja"])
    assert result.action == "block", (
        f"injection/ja: expected block, got {result.action!r}"
    )


@pytest.mark.asyncio
async def test_injection_soft_english(ps_client, scenarios):
    s = scenarios["injSoft"]
    result = await ps_client.protect_prompt(s["prompt"])
    assert result.action == "block", (
        f"injSoft/en: expected block, got {result.action!r}"
    )


@pytest.mark.asyncio
async def test_injection_soft_japanese(ps_client, scenarios):
    s = scenarios["injSoft"]
    result = await ps_client.protect_prompt(s["meta"]["prompt_ja"])
    assert result.action == "block", (
        f"injSoft/ja: expected block, got {result.action!r}"
    )
