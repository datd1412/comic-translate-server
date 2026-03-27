import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db, User
from auth_routes import get_current_user
from config import settings

router = APIRouter()


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    json_payload: dict,
    headers: dict | None = None,
    max_attempts: int = 3,
    timeout_seconds: float = 60.0,
) -> httpx.Response:
    """Retry upstream API calls on transient errors such as 429/5xx."""
    delay_seconds = 1.0
    last_response = None

    for attempt in range(1, max_attempts + 1):
        response = await client.post(url, json=json_payload, headers=headers, timeout=timeout_seconds)
        last_response = response

        # Success path
        if response.status_code < 400:
            return response

        # Retry only for rate limit / transient server errors
        if response.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay_seconds = max(delay_seconds, float(retry_after))
                except ValueError:
                    pass
            await asyncio.sleep(delay_seconds)
            delay_seconds *= 2
            continue

        response.raise_for_status()

    # Defensive fallback
    if last_response is not None:
        last_response.raise_for_status()
    raise HTTPException(status_code=500, detail="Unknown upstream error")

# Map UI model names to Gemini API model identifiers
GEMINI_API_MODELS = {
    "Gemini-3.0-Flash": "gemini-3-flash-preview",
    "Gemini-2.0-Flash": "gemini-2.0-flash",
    "Gemini-2.5-Pro": "gemini-2.5-pro",
}

DEEPSEEK_API_MODELS = {
    "Deepseek-v3": "deepseek-chat",
    "Deepseek-reasoner": "deepseek-reasoner",
}

# Simple credit cost logic - can be expanded
MODEL_COSTS = {
    "Gemini-3.0-Flash": 1,
    "Gemini-2.0-Flash": 1,
    "Gemini-2.0-Pro": 5,
    "GPT-4.1": 5,
    "GPT-4.1-mini": 1,
    "Claude 4.5 Sonnet": 5,
    "Claude 4.5 Haiku": 1,
    "Deepseek-v3": 1,
    "Custom": 1,
}

# OCR Costs
OCR_COSTS = {
    "Default": 2, 
    "Microsoft OCR": 2,
    "Google Cloud OCR": 2,
    "GPT-4.1-mini": 2,
    "GPT-4.1": 4,
    "Gemini-2.0-Flash": 2,
}

def deduct_credits_or_fail(user: User, cost: int, db: Session):
    # Check if user has enough credits
    if user.credits_total < cost:
        raise HTTPException(
            status_code=402, 
            detail={"type": "INSUFFICIENT_CREDITS", "message": f"You need {cost} credits but have {user.credits_total}."}
        )
    
    # Deduct logic (prioritize subscription, then one_time)
    remaining_cost = cost
    
    if user.credits_subscription >= remaining_cost:
        user.credits_subscription -= remaining_cost
        remaining_cost = 0
    else:
        remaining_cost -= user.credits_subscription
        user.credits_subscription = 0
        
    if remaining_cost > 0:
         user.credits_one_time -= remaining_cost
         
    user.update_total_credits()
    db.commit()


@router.post("/api/v1/translate")
async def proxy_translate(
    request: Request,
    user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    data = await request.json()
    model_name = str(data.get("translator", "Gemini-3.0-Flash") or "Gemini-3.0-Flash").strip()
    text_blocks = data.get("texts", [])
    
    cost_per_block = MODEL_COSTS.get(model_name, 1)
    total_cost = cost_per_block * len(text_blocks)

    # Snapshot credits so we can rollback exactly if upstream API fails.
    credits_before_subscription = user.credits_subscription
    credits_before_one_time = user.credits_one_time
    credits_before_total = user.credits_total
    
    # Deduct credits
    deduct_credits_or_fail(user, total_cost, db)
    
    result_blocks = []
    
    # NOTE: In a complete production system, this is where you would call the REAL 
    # Gemini / GPT / Claude APIs using YOUR API KEY (settings.GEMINI_API_KEY).
    # Since we are setting up the architecture, we will simulate the translation 
    # or pass it through if an API key is provided.
    
    is_deepseek_request = model_name in DEEPSEEK_API_MODELS or model_name.lower().startswith("deepseek")

    if is_deepseek_request:
        if not settings.DEEPSEEK_API_KEY:
            # Do not silently fall back to Gemini when user explicitly chose DeepSeek.
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "type": "PROVIDER_NOT_CONFIGURED",
                    "provider": "DeepSeek",
                    "message": "Bạn đã chọn DeepSeek nhưng DEEPSEEK_API_KEY chưa được cấu hình trong server.",
                },
            )

        # Normalize common aliases from UI/localization.
        deepseek_model = DEEPSEEK_API_MODELS.get(model_name, "deepseek-chat")

        configured_provider = (settings.DEEPSEEK_PROVIDER or "auto").strip().lower()
        deepseek_base_url = (settings.DEEPSEEK_BASE_URL or "https://api.deepseek.com/v1").strip().rstrip("/")
        auto_is_nvidia = settings.DEEPSEEK_API_KEY.startswith("nvapi-") or "nvidia.com" in deepseek_base_url
        use_nvidia_provider = configured_provider == "nvidia" or (configured_provider == "auto" and auto_is_nvidia)

        if use_nvidia_provider:
            if deepseek_model == "deepseek-chat":
                deepseek_model = settings.DEEPSEEK_NVIDIA_CHAT_MODEL
            elif deepseek_model == "deepseek-reasoner":
                deepseek_model = settings.DEEPSEEK_NVIDIA_REASONER_MODEL

        try:
            source_lang = data.get('source_language', 'Japanese')
            target_lang = data.get('target_language', 'English')
            url = f"{deepseek_base_url}/chat/completions"

            system_prompt = (
                f"You are an expert translator who translates {source_lang} to {target_lang}. "
                f"You pay attention to style, formality, idioms, slang etc and try to convey it "
                f"in the way a {target_lang} speaker would understand.\n"
                f"You will be translating text OCR'd from a comic. "
                f"You will receive a JSON object where each key is a block ID and the value is the text to translate. "
                f"Return a JSON object with the same keys and translated values. "
                f"If text is already in {target_lang} or looks like gibberish, output it as-is. "
                f"Do NOT add explanations. Only output the JSON."
            )

            blocks_dict = {}
            for block in text_blocks:
                blocks_dict[str(block.get('id', ''))] = block.get('text', '')

            import json as _json
            user_prompt = _json.dumps(blocks_dict, ensure_ascii=False)
            extra_context = data.get('extra_context', '')
            if extra_context:
                user_prompt = f"{extra_context}\n{user_prompt}"

            payload = {
                "model": deepseek_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
                "max_tokens": min(2048, max(512, len(text_blocks) * 256)),
            }

            headers = {
                "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            }

            async with httpx.AsyncClient() as client:
                response = await post_with_retry(
                    client,
                    url,
                    json_payload=payload,
                    headers=headers,
                    timeout_seconds=settings.DEEPSEEK_TIMEOUT_SECONDS,
                )
                res_json = response.json()
                raw_text = res_json['choices'][0]['message']['content'].strip()

                import re
                json_match = re.search(r'\{[\s\S]*\}', raw_text)
                if json_match:
                    translations_dict = _json.loads(json_match.group(0))
                else:
                    translations_dict = _json.loads(raw_text)

                for block in text_blocks:
                    block_id = str(block.get('id', ''))
                    result_blocks.append({
                        "id": block.get("id"),
                        "translation": translations_dict.get(block_id, block.get('text', ''))
                    })

        except httpx.HTTPStatusError as e:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()

            deepseek_error_text = e.response.text or ""
            deepseek_error_lower = deepseek_error_text.lower()
            if "insufficient balance" in deepseek_error_lower or "insufficient_balance" in deepseek_error_lower:
                raise HTTPException(
                    status_code=402,
                    detail={
                        "type": "UPSTREAM_INSUFFICIENT_BALANCE",
                        "provider": "DeepSeek",
                        "message": "Tài khoản DeepSeek của server đã hết số dư. Vui lòng nạp tiền DeepSeek hoặc đổi model.",
                    },
                )

            if e.response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "type": "UPSTREAM_RATE_LIMIT",
                        "provider": "DeepSeek",
                        "message": "DeepSeek đang quá tải hoặc vượt hạn mức. Vui lòng thử lại sau.",
                    },
                )
            if e.response.status_code == 403:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "type": "UPSTREAM_AUTHORIZATION_FAILED",
                        "provider": "DeepSeek",
                        "message": "Upstream từ chối quyền truy cập model/provider. Hãy kiểm tra API key và quyền model trên NVIDIA Build.",
                        "upstream": deepseek_error_text,
                    },
                )
            raise HTTPException(status_code=502, detail=f"AI API Error (DeepSeek): {e.response.text}")

        except httpx.TimeoutException:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()
            raise HTTPException(
                status_code=504,
                detail={
                    "type": "UPSTREAM_TIMEOUT",
                    "provider": "DeepSeek",
                    "message": "DeepSeek phản hồi quá chậm và bị timeout. Vui lòng thử lại.",
                },
            )

        except httpx.RequestError as e:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()
            raise HTTPException(
                status_code=502,
                detail={
                    "type": "UPSTREAM_NETWORK_ERROR",
                    "provider": "DeepSeek",
                    "message": f"Lỗi kết nối tới DeepSeek/NVIDIA: {repr(e)}",
                },
            )

        except HTTPException:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()
            raise

        except Exception as e:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()
            raise HTTPException(status_code=500, detail=f"AI API Error (DeepSeek): {repr(e)}")

    elif settings.GEMINI_API_KEY:
        try:
            source_lang = data.get('source_language', 'Japanese')
            target_lang = data.get('target_language', 'English')
            gemini_model = GEMINI_API_MODELS.get(model_name, "gemini-2.0-flash")
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={settings.GEMINI_API_KEY}"

            system_prompt = (
                f"You are an expert translator who translates {source_lang} to {target_lang}. "
                f"You pay attention to style, formality, idioms, slang etc and try to convey it "
                f"in the way a {target_lang} speaker would understand.\n"
                f"You will be translating text OCR'd from a comic. "
                f"You will receive a JSON object where each key is a block ID and the value is the text to translate. "
                f"Return a JSON object with the same keys and translated values. "
                f"If text is already in {target_lang} or looks like gibberish, output it as-is. "
                f"Do NOT add explanations. Only output the JSON."
            )

            # Build a single JSON with all blocks for batch translation
            blocks_dict = {}
            for block in text_blocks:
                blocks_dict[str(block.get('id', ''))] = block.get('text', '')

            import json as _json
            user_prompt = _json.dumps(blocks_dict, ensure_ascii=False)
            extra_context = data.get('extra_context', '')
            if extra_context:
                user_prompt = f"{extra_context}\n{user_prompt}"

            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"parts": [{"text": user_prompt}]}],
                "safetySettings": [
                    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
                    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
                ],
                "generationConfig": {
                    "temperature": 1.0,
                    "maxOutputTokens": 5000,
                },
            }

            async with httpx.AsyncClient() as client:
                response = await post_with_retry(client, url, json_payload=payload)
                res_json = response.json()
                raw_text = res_json['candidates'][0]['content']['parts'][0]['text'].strip()

                # Parse JSON from response (handle markdown code blocks)
                import re
                json_match = re.search(r'\{[\s\S]*\}', raw_text)
                if json_match:
                    translations_dict = _json.loads(json_match.group(0))
                else:
                    translations_dict = _json.loads(raw_text)

                for block in text_blocks:
                    block_id = str(block.get('id', ''))
                    result_blocks.append({
                        "id": block.get("id"),
                        "translation": translations_dict.get(block_id, block.get('text', ''))
                    })
                    
        except httpx.HTTPStatusError as e:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()

            if e.response.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail={
                        "type": "UPSTREAM_RATE_LIMIT",
                        "provider": "Gemini",
                        "message": "Gemini vượt hạn mức hoặc đang quá tải. Hãy thử lại sau hoặc chọn model Deepseek-v3.",
                    },
                )
            raise HTTPException(status_code=502, detail=f"AI API Error (Gemini): {e.response.text}")

        except Exception as e:
            user.credits_subscription = credits_before_subscription
            user.credits_one_time = credits_before_one_time
            user.credits_total = credits_before_total
            db.commit()
            raise HTTPException(status_code=500, detail=f"AI API Error (Gemini): {str(e)}")
            
    else:
        # Mock translation (no API key provided)
        for block in text_blocks:
            result_blocks.append({
                "id": block.get("id"),
                "translation": f"[Custom Server:] {block.get('text', '')}"
            })
    
    return {
        "translations": result_blocks,
        "credits": {
            "subscription": user.credits_subscription,
            "one_time": user.credits_one_time,
            "total": user.credits_total
        }
    }


@router.post("/api/v1/ocr")
async def proxy_ocr(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    data = await request.json()
    ocr_name = data.get("ocr_name", "Default")
    
    # OCR is usually charged per page/request
    cost = OCR_COSTS.get(ocr_name, 2)
    deduct_credits_or_fail(user, cost, db)
    
    # MOCK OCR Response. To make this real, you decode data.get("image_base64"),
    # send it to Google Cloud Vision or GPT-4o, and parse the bounding boxes.
    # The desktop app expects a list of dicts with 'text' and 'coordinates'
    
    mock_results = []
    coords = data.get("coordinates", [])
    if coords:
        # LLM OCR block-by-block mock
        for c in coords:
            mock_results.append({
                "text": "Server Mock OCR Text",
                "coordinates": c
            })
    else:
        # Full page mock
        mock_results.append({
            "text": "Mock Full Page Text",
            "coordinates": [10, 10, 100, 50]
        })
        
    return {
        "ocr_results": mock_results,
        "credits": {
            "subscription": user.credits_subscription,
            "one_time": user.credits_one_time,
            "total": user.credits_total
        }
    }
