"""
Real PS API integration tests for translated demo scenarios.

Requires env vars:
  PS_BASE_URL  — e.g. https://your-tenant.promptsecurity.ai
  PS_APP_ID    — APP-ID header value

Without these vars, all tests skip (CI shows yellow, not red).

Policy is passed per-request from tests/fixtures/ps_policy_reference.json.
entity_types is overridden per country so that country-specific PII is scanned,
not just EMAIL_ADDRESS (the default in the shared app policy).
Language Detector is disabled for non-English tests to prevent blocking
translated prompts that aren't in the policy's allowed-languages list.

Coverage guard: any scenario in scenarios.json with a meta.prompt_XX key must
appear in _registered_translations() — pytest.fail() if not.
"""

import copy
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
from prompt_security import PromptSecurityClient  # noqa: E402

SCENARIOS_PATH = Path(__file__).parent.parent / "app" / "data" / "scenarios.json"
POLICY_PATH = Path(__file__).parent / "fixtures" / "ps_policy_reference.json"

# Entity types to scan per country (all entities from reference policy thresholds)
COUNTRY_ENTITIES = {
    "JP": [
        "JAPAN_MY_NUMBER_PERSONAL", "JAPAN_MY_NUMBER_CORPORATE",
        "JAPAN_PASSPORT_NUMBER", "JAPAN_DRIVER_LICENSE_NUMBER",
        "JAPAN_BANK_ACCOUNT_NUMBER", "JAPAN_SOCIAL_INSURANCE_NUMBER_SIN",
        "JAPAN_RESIDENCE_CARD_NUMBER", "JAPAN_RESIDENT_REGISTRATION_NUMBER",
    ],
    "DE": [
        "GERMANY_ID_NUMBER", "GERMANY_PASSPORT_NUMBER",
        "GERMANY_DRIVERS_LICENSE_NUMBER", "GERMANY_TAX_ID_NUMBER", "GERMANY_VAT_NUMBER",
    ],
    "IN": ["INDIA_AADHAAR_NUMBER", "INDIA_PAN_NUMBER"],
    "IL": ["IL_ID_NUMBER", "IL_PASSPORT_RE", "IL_BANK_NUMBER", "IBAN_CODE"],
    "SG": ["SG_NRIC_FIN", "SINGAPORE_PASSPORT_NUMBER", "SINGAPORE_DRIVER_LICENSE_NUMBER"],
    "BR": ["BR_CPF_NUMBER", "BRAZIL_CNPJ_NUMBER"],
    "MY": ["MALAYSIA_ID_NUMBER"],
}


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


@pytest.fixture(scope="session")
def base_policy():
    with open(POLICY_PATH, encoding="utf-8") as f:
        return json.load(f)


def _pii_policy(base_policy: dict, country_code: str, native_lang: bool = False) -> dict:
    """
    Build a per-request policy for a PII country scan.
    - Sets entity_types to EMAIL_ADDRESS + all country-specific entities
    - Disables Language Detector for native-language tests (prevents blocking non-EN prompts)
    """
    policy = copy.deepcopy(base_policy)
    sd = policy["prompt"]["Sensitive Data"]
    sd["entity_types"] = ["EMAIL_ADDRESS"] + COUNTRY_ENTITIES.get(country_code, [])
    if native_lang:
        policy["prompt"]["Language Detector"]["enabled"] = False
    return policy


def _findings_entities(result) -> set:
    found = set()
    findings = result.raw.get("result", {}).get("prompt", {}).get("findings", {})
    for detections in findings.values():
        if not isinstance(detections, list):
            continue
        for d in detections:
            if isinstance(d, dict) and "entity_type" in d:
                found.add(d["entity_type"])
    return found


async def _assert_pii_modify(ps_client, scenarios, key, lang, policy):
    s = scenarios[key]
    prompt = s["meta"][f"prompt_{lang}"] if lang != "en" else s["prompt"]
    result = await ps_client.protect_prompt(prompt, policy=policy)
    detected = _findings_entities(result)
    assert result.action == "modify", (
        f"{key}/{lang}: expected action=modify, got {result.action!r}. "
        f"Detected: {detected or '(none)'}"
    )
    return detected


# ── Coverage guard ────────────────────────────────────────────────────────────

def _registered_translations():
    return {
        ("pii_JP", "ja"), ("pii_DE", "de"), ("pii_IN", "hi"),
        ("pii_IL", "he"), ("pii_SG", "zh"), ("pii_BR", "pt"),
        ("pii_MY", "ms"), ("injection", "ja"), ("injSoft", "ja"),
    }


def test_translation_coverage():
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
async def test_pii_japan_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "JP", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_JP", "en", policy)
    print(f"\npii_JP/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_japan_japanese(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "JP", native_lang=True)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_JP", "ja", policy)
    print(f"\npii_JP/ja detected: {detected}")


# ── Germany ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_germany_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "DE", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_DE", "en", policy)
    print(f"\npii_DE/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_germany_german(ps_client, scenarios, base_policy):
    # German is in the allowed list — Language Detector stays on
    policy = _pii_policy(base_policy, "DE", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_DE", "de", policy)
    print(f"\npii_DE/de detected: {detected}")


# ── India ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_india_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IN", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IN", "en", policy)
    print(f"\npii_IN/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_india_hindi(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IN", native_lang=True)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IN", "hi", policy)
    print(f"\npii_IN/hi detected: {detected}")


# ── Israel ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_israel_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IL", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IL", "en", policy)
    print(f"\npii_IL/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_israel_hebrew(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "IL", native_lang=True)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_IL", "he", policy)
    print(f"\npii_IL/he detected: {detected}")


# ── Singapore ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_singapore_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "SG", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_SG", "en", policy)
    print(f"\npii_SG/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_singapore_mandarin(ps_client, scenarios, base_policy):
    # Chinese is explicitly denied in Language Detector — must disable
    policy = _pii_policy(base_policy, "SG", native_lang=True)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_SG", "zh", policy)
    print(f"\npii_SG/zh detected: {detected}")


# ── Brazil ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_brazil_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "BR", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_BR", "en", policy)
    print(f"\npii_BR/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_brazil_portuguese(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "BR", native_lang=True)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_BR", "pt", policy)
    print(f"\npii_BR/pt detected: {detected}")


# ── Malaysia ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_malaysia_english(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "MY", native_lang=False)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_MY", "en", policy)
    print(f"\npii_MY/en detected: {detected}")


@pytest.mark.asyncio
async def test_pii_malaysia_malay(ps_client, scenarios, base_policy):
    policy = _pii_policy(base_policy, "MY", native_lang=True)
    detected = await _assert_pii_modify(ps_client, scenarios, "pii_MY", "ms", policy)
    print(f"\npii_MY/ms detected: {detected}")


# ── Prompt Injection ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_injection_english(ps_client, scenarios, base_policy):
    s = scenarios["injection"]
    result = await ps_client.protect_prompt(s["prompt"], policy=base_policy)
    assert result.action == "block", f"injection/en: expected block, got {result.action!r}"


@pytest.mark.asyncio
async def test_injection_japanese(ps_client, scenarios, base_policy):
    policy = copy.deepcopy(base_policy)
    policy["prompt"]["Language Detector"]["enabled"] = False
    s = scenarios["injection"]
    result = await ps_client.protect_prompt(s["meta"]["prompt_ja"], policy=policy)
    assert result.action == "block", f"injection/ja: expected block, got {result.action!r}"


@pytest.mark.asyncio
async def test_injection_soft_english(ps_client, scenarios, base_policy):
    s = scenarios["injSoft"]
    result = await ps_client.protect_prompt(s["prompt"], policy=base_policy)
    assert result.action == "block", f"injSoft/en: expected block, got {result.action!r}"


@pytest.mark.asyncio
async def test_injection_soft_japanese(ps_client, scenarios, base_policy):
    policy = copy.deepcopy(base_policy)
    policy["prompt"]["Language Detector"]["enabled"] = False
    s = scenarios["injSoft"]
    result = await ps_client.protect_prompt(s["meta"]["prompt_ja"], policy=policy)
    assert result.action == "block", f"injSoft/ja: expected block, got {result.action!r}"
