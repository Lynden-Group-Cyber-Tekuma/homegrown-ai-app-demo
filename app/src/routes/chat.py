"""Streaming chat route with Prompt Security scanning."""
import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
import openai
from fastapi import APIRouter, Depends, HTTPException, Request
from openai import AsyncOpenAI
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import get_current_user
from crypto import decrypt
from database import get_db
from models import ChatSession, Message, User
from prompt_security import PromptSecurityClient
from schemas import ChatRequest
from src.llm import catalog, routing
from src.services import ps
from src.services.audit import _log_msg
from src.services.serializers import _build_content
from token_counter import estimate_message_tokens, estimate_text_tokens

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Chat streaming ────────────────────────────────────────────────────────────
@router.post("/chat/stream")
async def chat_stream(
    request: ChatRequest,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not request.messages:
        raise HTTPException(status_code=400, detail="messages list cannot be empty")

    available = catalog._model_cache or catalog._FALLBACK_MODELS
    model = request.model or available[0]["id"]
    if not model:
        raise HTTPException(status_code=503, detail="No models available — save a provider API key in Admin → Settings → LLM Keys")
    if current_user.allowed_models is not None and model not in current_user.allowed_models:
        raise HTTPException(status_code=403, detail="Model not allowed for this user")

    last_user_msg = next((m.content for m in reversed(request.messages) if m.role == "user"), None)
    if not last_user_msg:
        raise HTTPException(status_code=400, detail="No user message found")

    system_prompt = request.system_prompt or "You are a helpful AI assistant."

    # ── Daily limit check ─────────────────────────────────────────────────────
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    used_today = await db.scalar(
        select(func.count(Message.id)).where(
            Message.user_id == current_user.id,
            Message.role == "user",
            Message.created_at >= today_start,
        )
    ) or 0
    if current_user.daily_message_limit and used_today >= current_user.daily_message_limit:
        raise HTTPException(
            status_code=429,
            detail={"used": used_today, "limit": current_user.daily_message_limit},
        )

    # ── Ensure session exists ─────────────────────────────────────────────────
    session_id = str(request.session_id) if request.session_id else str(uuid.uuid4())
    session = await db.scalar(select(ChatSession).where(ChatSession.id == session_id))
    if session and session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    if not session:
        title = last_user_msg[:60] + ("…" if len(last_user_msg) > 60 else "")
        session = ChatSession(id=session_id, user_id=current_user.id, title=title)
        db.add(session)
        await db.commit()

    # ── Per-user PS client / mode ─────────────────────────────────────────────
    ps_mode        = current_user.ps_mode or "api"
    ps_client: Optional[PromptSecurityClient] = None
    ps_gw_client: Optional[AsyncOpenAI] = None    # gateway-mode LLM client (OpenAI-compat providers)
    ps_gw_gemini: Optional[dict]        = None    # gateway-mode config for Gemini (native path)
    ps_gw_anthropic: Optional[dict]     = None    # gateway-mode config for Anthropic (native path)

    if current_user.ps_enabled and current_user.ps_tenant and current_user.ps_api_key_enc:
        try:
            ps_app_id = decrypt(current_user.ps_api_key_enc)
        except ValueError:
            logger.warning("PS key decrypt failed for %s — key rotated? Ask user to re-enter PS key.", current_user.email)
            ps_app_id = None
        if ps_app_id:
            try:
                if ps_mode == "api":
                    ps_client = ps.PromptSecurityClient(
                        base_url=current_user.ps_tenant.base_url,
                        app_id=ps_app_id,
                    )
                elif ps_mode == "gateway" and current_user.ps_tenant.gateway_url:
                    llm_key = routing._get_llm_key(current_user, model)
                    gw_host = current_user.ps_tenant.gateway_url.rstrip('/')
                    gw_base = gw_host if gw_host.endswith('/v1') else gw_host + '/v1'
                    logger.info("PS gateway init for %s → %s model=%s (llm_key set: %s)",
                                current_user.email, gw_base, model, bool(llm_key))
                    ps_root = gw_host[:-3] if gw_host.endswith('/v1') else gw_host
                    if llm_key:
                        if model.startswith('gemini-'):
                            gemini_url = f"{ps_root}/v1beta/models/{model}:generateContent"
                            logger.info("PS Gemini gateway URL → %s", gemini_url)
                            ps_gw_gemini = {'url': gemini_url, 'llm_key': llm_key}
                        elif model.startswith('claude-'):
                            anthropic_url = f"{ps_root}/v1/messages"
                            logger.info("PS Anthropic gateway URL → %s model=%s", anthropic_url, model)
                            ps_gw_anthropic = {'url': anthropic_url, 'llm_key': llm_key, 'model': model}
                        else:
                            # PS routes OpenAI/Perplexity via LLM API key alone (OpenAI-compat)
                            logger.info("PS gateway base_url → %s model=%s", gw_base, model)
                            ps_gw_client = AsyncOpenAI(
                                api_key=llm_key,
                                base_url=gw_base,
                                timeout=30.0,
                                default_headers={"ps-user": current_user.email},
                            )
                    else:
                        logger.warning("Gateway mode: no LLM key for %s — PS gateway requires the provider API key", current_user.email)
            except Exception as e:
                logger.warning("Could not init PS client for %s: %s", current_user.email, e)

    # skip_ps is used by compare mode to get a raw LLM response alongside the PS-scanned one.
    # Any authenticated user may use it — restricting to admins broke compare mode for SE users.

    # ── Store user message in DB ──────────────────────────────────────────────
    user_db_msg = Message(
        session_id=session_id,
        user_id=current_user.id,
        role="user",
        content=last_user_msg,
        model=model,
    )
    db.add(user_db_msg)
    await db.commit()

    skip_ps = request.skip_ps

    async def generate():
        reply = ""
        prompt_action = "pass"
        t0 = time.monotonic()
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None
        total_tokens: Optional[int] = None
        ps_prompt_raw: Optional[dict] = None
        ps_resp_raw: Optional[dict] = None

        msgs = list(request.messages)
        payload = [{"role": "system", "content": system_prompt}] + [
            {"role": m.role, "content": _build_content(m)} for m in msgs
        ]

        # ── Gateway mode: route through PS proxy, skip explicit scanning ───────
        if ps_gw_gemini and not skip_ps:
            system_parts, contents = [], []
            for msg in payload:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict))
                if role == 'system':
                    system_parts.append({"text": content})
                else:
                    contents.append({"role": "user" if role == "user" else "model",
                                     "parts": [{"text": content}]})
            gemini_body: dict = {"contents": contents}
            if system_parts:
                gemini_body["system_instruction"] = {"parts": system_parts}
            try:
                async with httpx.AsyncClient(timeout=10.0) as hclient:
                    resp = await hclient.post(ps_gw_gemini['url'],
                        headers={
                            "Content-Type": "application/json",
                            "x-goog-api-key": ps_gw_gemini['llm_key'],
                            "ps-user": current_user.email,
                        },
                        json=gemini_body,
                    )
                    if resp.status_code != 200:
                        raise Exception(f"Gemini gateway {resp.status_code}: {resp.text[:300]}")
                    data = resp.json()
                    text = data['candidates'][0]['content']['parts'][0].get('text', '')
                    if text:
                        reply = text
                        yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
            except Exception as e:
                detail = f"Gateway error: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            resp_ms = round((time.monotonic() - t0) * 1000)
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0})}\n\n"
            return

        if ps_gw_anthropic and not skip_ps:
            anthropic_messages = []
            system_text = ""
            for msg in payload:
                role = msg.get('role', 'user')
                content = msg.get('content', '')
                if isinstance(content, list):
                    content = ' '.join(p.get('text', '') for p in content if isinstance(p, dict))
                if role == 'system':
                    system_text = content
                else:
                    anthropic_messages.append({"role": role, "content": content})
            anthropic_body: dict = {
                "model": ps_gw_anthropic['model'],
                "messages": anthropic_messages,
                "max_tokens": 1024,
                "stream": True,
            }
            if system_text:
                anthropic_body["system"] = system_text
            try:
                async with httpx.AsyncClient(timeout=30.0) as hclient:
                    async with hclient.stream('POST', ps_gw_anthropic['url'],
                        headers={
                            "Content-Type": "application/json",
                            "x-api-key": ps_gw_anthropic['llm_key'],
                            "anthropic-version": "2023-06-01",
                            "ps-user": current_user.email,
                        },
                        json=anthropic_body,
                    ) as resp:
                        if resp.status_code != 200:
                            body_bytes = await resp.aread()
                            raise Exception(f"Anthropic gateway {resp.status_code}: {body_bytes[:300]}")
                        async for line in resp.aiter_lines():
                            if await http_request.is_disconnected():
                                logger.info(
                                    "Client disconnected — aborting Anthropic gateway stream (user=%s)",
                                    current_user.email,
                                )
                                return
                            if not line.startswith('data: '):
                                continue
                            data_str = line[6:]
                            try:
                                event = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            etype = event.get('type')
                            if etype == 'content_block_delta':
                                delta = event.get('delta', {})
                                if delta.get('type') == 'text_delta':
                                    text = delta.get('text', '')
                                    if text:
                                        reply += text
                                        yield f"data: {json.dumps({'type': 'token', 'content': text})}\n\n"
                            elif etype == 'message_start':
                                u = event.get('message', {}).get('usage', {})
                                prompt_tokens = u.get('input_tokens', 0)
                            elif etype == 'message_delta':
                                u = event.get('usage', {})
                                completion_tokens = u.get('output_tokens', 0)
            except Exception as e:
                detail = f"Anthropic gateway error: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            resp_ms = round((time.monotonic() - t0) * 1000)
            prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
            completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
            total_tokens = (prompt_tokens or 0) + (completion_tokens or 0)
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens})}\n\n"
            return

        if ps_gw_client and not skip_ps:
            try:
                prompt_tokens = estimate_message_tokens(payload, model=model)
                stream = await ps_gw_client.chat.completions.create(
                    model=model, messages=payload, stream=True
                )
                async for chunk in stream:
                    if await http_request.is_disconnected():
                        logger.info(
                            "Client disconnected — aborting PS gateway stream (user=%s)",
                            current_user.email,
                        )
                        return
                    if getattr(chunk, "usage", None):
                        prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                        completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                        total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        reply += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            except openai.BadRequestError as e:
                body = str(e).lower()
                logger.error("PS gateway 400 error: %s", e)
                # Only treat as a PS policy block if the body explicitly says so
                if "block" in body or "policy" in body or "violat" in body or "denied" in body:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block")
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': []})}\n\n"
                else:
                    detail = f"Gateway config error (400): {e}"
                    logger.error(detail)
                    yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            except openai.AuthenticationError as e:
                detail = f"Gateway auth failed — check PS App ID: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return
            except openai.PermissionDeniedError as e:
                body = str(e).lower()
                logger.error("PS gateway 403: %s", e)
                if "block" in body or "policy" in body or "violat" in body:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block")
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': []})}\n\n"
                else:
                    yield f"data: {json.dumps({'type': 'error', 'detail': f'Gateway permission denied: {e}'})}\n\n"
                return
            except Exception as e:
                detail = f"Gateway error: {e}"
                logger.error(detail)
                yield f"data: {json.dumps({'type': 'error', 'detail': detail})}\n\n"
                return

            resp_ms = round((time.monotonic() - t0) * 1000)
            completion_tokens = completion_tokens or estimate_text_tokens(reply, model=model)
            prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=model)
            total_tokens = total_tokens or (prompt_tokens + completion_tokens)
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=True, ps_action="pass", response_ms=resp_ms)
            today_used = used_today + 1
            yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': True, 'ps_action': 'gateway', 'ps_violations': [], 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens})}\n\n"
            return

        # ── API mode: explicit PS scan + direct provider call ─────────────────
        prompt_violations: list = []
        if ps_client and not skip_ps:
            try:
                prompt_tok_est = estimate_text_tokens(last_user_msg)
                logger.info("PS prompt scan: user=%s, prompt_chars=%d, estimated_tokens=%d, prompt_preview=%.120s",
                            current_user.email, len(last_user_msg), prompt_tok_est, last_user_msg)
                ps_result = await ps_client.protect_prompt(
                    user_prompt=last_user_msg,
                    system_prompt=system_prompt,
                    user=current_user.email,
                )
                logger.info("PS prompt result: action=%s, allowed=%s, violations=%s, modified=%s",
                            ps_result.action, ps_result.allowed, ps_result.violations, bool(ps_result.modified_text))
                prompt_violations = ps_result.violations
                ps_prompt_raw = {"request": ps_result.raw_request, "response": ps_result.raw}
                if not ps_result.allowed:
                    await _log_msg(db, session_id, current_user.id, "assistant", "", model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block",
                                   ps_violations=ps_result.violations)
                    yield f"data: {json.dumps({'type': 'blocked', 'action': 'block', 'violations': ps_result.violations, 'ps_raw': {'prompt': ps_prompt_raw}})}\n\n"
                    return
                if ps_result.modified_text:
                    last_user_msg_eff = ps_result.modified_text
                    prompt_action = "modify"
                    for i in range(len(payload) - 1, -1, -1):
                        if payload[i]["role"] == "user":
                            payload[i]["content"] = last_user_msg_eff
                            break
                else:
                    last_user_msg_eff = last_user_msg
            except Exception as e:
                logger.error("PS prompt scan error for %s: %s", current_user.email, e)
                err_hint = "PS App ID may be wrong for this tenant — re-enter it in ⚙ Settings → Prompt Security."
                yield ("data: " + json.dumps({'type': 'error', 'detail': f'PS scan failed: {e}. {err_hint}'}) + "\n\n")
                return
        else:
            last_user_msg_eff = last_user_msg

        # ── Stream (direct provider call: per-user key or shared key) ──────────
        try:
            llm, effective_model = routing._user_llm_client(current_user, model)
            prompt_tokens = estimate_message_tokens(payload, model=effective_model)

            stream = await llm.chat.completions.create(
                model=effective_model,
                messages=payload,
                stream=True,
                stream_options={"include_usage": True},
            )

            # ── Cancellation-aware drain loop ─────────────────────────────────
            # A plain `async for chunk in stream` blocks inside httpx recv()
            # while waiting for the next token — is_disconnected() can only fire
            # between chunks.  Instead we pump chunks into a queue from a
            # background task and drain with a short timeout so we can check for
            # disconnection even when the model is slow.
            _STREAM_DONE = object()
            chunk_queue: asyncio.Queue = asyncio.Queue()

            async def _pump():
                try:
                    async for chunk in stream:
                        await chunk_queue.put(chunk)
                except asyncio.CancelledError:
                    pass  # expected — client disconnected
                except Exception as exc:
                    logger.debug("Stream pump error: %s", exc)
                finally:
                    await chunk_queue.put(_STREAM_DONE)

            pump_task = asyncio.create_task(_pump())
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(chunk_queue.get(), timeout=0.4)
                    except asyncio.TimeoutError:
                        # No token arrived in 0.4 s — check if the client left
                        if await http_request.is_disconnected():
                            logger.info(
                                "Client disconnected — cancelling generation "
                                "(user=%s, model=%s)", current_user.email, effective_model,
                            )
                            pump_task.cancel()
                            return
                        continue  # still connected, keep waiting

                    if chunk is _STREAM_DONE:
                        break  # stream finished normally

                    if getattr(chunk, "usage", None):
                        prompt_tokens = getattr(chunk.usage, "prompt_tokens", prompt_tokens)
                        completion_tokens = getattr(chunk.usage, "completion_tokens", completion_tokens)
                        total_tokens = getattr(chunk.usage, "total_tokens", total_tokens)
                    if chunk.choices and chunk.choices[0].delta.content:
                        token = chunk.choices[0].delta.content
                        reply += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
            finally:
                if not pump_task.done():
                    pump_task.cancel()
                    try:
                        await pump_task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            logger.error("LLM stream error: %s", e)
            yield f"data: {json.dumps({'type': 'error', 'detail': str(e)})}\n\n"
            return

        resp_ms = round((time.monotonic() - t0) * 1000)

        # ── PS: scan response ─────────────────────────────────────────────────
        final_action = prompt_action
        ps_violations: list = list(prompt_violations)
        if ps_client and not skip_ps and reply:
            try:
                ps_resp = await ps_client.protect_response(
                    response_text=reply,
                    user=current_user.email,
                )
                ps_violations = prompt_violations + ps_resp.violations
                ps_resp_raw = {"request": ps_resp.raw_request, "response": ps_resp.raw}
                if not ps_resp.allowed:
                    await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                                   ps_scanned=True, ps_blocked=True, ps_action="block",
                                   ps_violations=ps_violations, response_ms=resp_ms)
                    yield f"data: {json.dumps({'type': 'revoke', 'action': 'block', 'violations': ps_violations, 'ps_raw': {'prompt': ps_prompt_raw, 'response': ps_resp_raw}})}\n\n"
                    return
                if ps_resp.modified_text:
                    reply = ps_resp.modified_text
                    final_action = "modify"
                    yield f"data: {json.dumps({'type': 'sanitized', 'text': reply})}\n\n"
            except Exception as e:
                logger.error("PS response scan error for %s: %s", current_user.email, e)
                err_hint = "PS App ID may be wrong for this tenant — re-enter it in ⚙ Settings → Prompt Security."
                yield ("data: " + json.dumps({'type': 'error', 'detail': f'PS response scan failed: {e}. {err_hint}'}) + "\n\n")
                return

        completion_tokens = completion_tokens or estimate_text_tokens(reply, model=effective_model)
        prompt_tokens = prompt_tokens or estimate_message_tokens(payload, model=effective_model)
        total_tokens = total_tokens or (prompt_tokens + completion_tokens)

        ps_active = bool(ps_client) and not skip_ps
        try:
            await _log_msg(db, session_id, current_user.id, "assistant", reply, model,
                           ps_scanned=ps_active, ps_action=final_action,
                           ps_violations=ps_violations, response_ms=resp_ms)
        except Exception as e:
            logger.error("Failed to log assistant message: %s", e)

        today_used = used_today + 1
        ps_raw_payload = {"prompt": ps_prompt_raw, "response": ps_resp_raw} if ps_active else None
        yield f"data: {json.dumps({'type': 'done', 'model': model, 'session_id': session_id, 'ps_scanned': ps_active, 'ps_action': final_action, 'ps_violations': ps_violations, 'messages_today': today_used, 'daily_limit': current_user.daily_message_limit, 'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': total_tokens, 'ps_raw': ps_raw_payload})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
