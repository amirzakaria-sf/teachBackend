from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from urllib.parse import urlparse

from django.conf import settings


def _get_setting(name: str, default: str = "") -> str:
    return str(getattr(settings, name, os.getenv(name, default))).strip()


def _safe_json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _speech_locales() -> list[str]:
    raw = _get_setting("AZURE_SPEECH_LOCALES", "en-US,hi-IN")
    locales = [item.strip() for item in raw.split(",") if item.strip()]
    return locales or ["en-US"]


def _prioritize_locales(*, source_hint: str, locales: list[str]) -> list[str]:
    hint = (source_hint or "").lower()
    preferred: list[str] = []
    if "hindi" in hint or "_hi" in hint or "-hi" in hint:
        preferred = ["hi-IN", "en-US"]
    elif "english" in hint or "_en" in hint or "-en" in hint:
        preferred = ["en-US", "hi-IN"]

    ordered: list[str] = []
    for locale in preferred + locales:
        if locale and locale not in ordered:
            ordered.append(locale)
    return ordered or locales


def _language_code_from_locale(locale: str) -> str:
    normalized = (locale or "").strip().lower()
    if not normalized:
        return "en"
    if "-" in normalized:
        return normalized.split("-", 1)[0]
    return normalized


def _speech_poll_attempts() -> int:
    raw = _get_setting("AZURE_SPEECH_POLL_ATTEMPTS", "72")
    try:
        value = int(raw)
    except ValueError:
        return 72
    return max(value, 12)


def _chunk_text_for_translation(text: str, *, max_chars: int = 4500) -> list[str]:
    normalized = (text or "").strip()
    if not normalized:
        return [""]
    if len(normalized) <= max_chars:
        return [normalized]

    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + max_chars, len(normalized))
        if end < len(normalized):
            split = normalized.rfind(" ", start, end)
            if split > start + int(max_chars * 0.6):
                end = split
        piece = normalized[start:end].strip()
        if piece:
            chunks.append(piece)
        start = end
    return chunks or [normalized]


def call_gpt_model(*, deployment_name: str, system_prompt: str, user_prompt: str, max_output_tokens: int = 1800) -> str:
    """Call Azure AI Foundry OpenAI-compatible endpoint.

    Supports API key auth first (as requested for project endpoint). If not configured,
    it tries Azure AD token auth through azure.identity.
    """

    endpoint = _get_setting("AZURE_OPENAI_ENDPOINT", "https://basic-agent-flow-zak-resource.services.ai.azure.com/openai/v1")
    api_key = _get_setting("AZURE_OPENAI_API_KEY")
    aad_scope = _get_setting("AZURE_OPENAI_SCOPE", "https://ai.azure.com/.default")

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - dependency-driven
        raise RuntimeError("openai package is not installed.") from exc

    client_kwargs: dict[str, Any] = {"base_url": endpoint}
    if api_key:
        client_kwargs["api_key"] = api_key
    else:
        try:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        except Exception as exc:  # pragma: no cover - dependency-driven
            raise RuntimeError(
                "AZURE_OPENAI_API_KEY is not configured and azure.identity is unavailable for AAD auth."
            ) from exc
        token_provider = get_bearer_token_provider(DefaultAzureCredential(), aad_scope)
        client_kwargs["api_key"] = token_provider

    client = OpenAI(**client_kwargs)
    response = client.responses.create(
        model=deployment_name,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
        max_output_tokens=max_output_tokens,
    )

    if hasattr(response, "output_text") and response.output_text:
        return response.output_text

    if hasattr(response, "output"):
        output = response.output
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                content = getattr(item, "content", None)
                if not content:
                    continue
                for block in content:
                    text = getattr(block, "text", None)
                    if text:
                        parts.append(text)
            if parts:
                return "\n".join(parts)
    return str(response)


def translate_text(*, text: str, from_lang: str = "en", to_lang: str) -> str:
    endpoint = _get_setting("AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com")
    api_key = _get_setting("AZURE_TRANSLATOR_KEY")
    api_version = _get_setting("AZURE_TRANSLATOR_API_VERSION", "2025-10-01-preview")
    region = _get_setting("AZURE_TRANSLATOR_REGION")
    if not api_key:
        raise RuntimeError("AZURE_TRANSLATOR_KEY is required for translation.")

    endpoints = [endpoint]
    speech_endpoint = _get_setting("AZURE_SPEECH_ENDPOINT")
    if speech_endpoint and speech_endpoint not in endpoints:
        endpoints.append(speech_endpoint)

    # Preferred: Translator preview endpoint (as provided by Azure sample)
    parts = _chunk_text_for_translation(text)
    translated_parts: list[str] = []
    last_error = ""
    for part in parts:
        translated = _translate_single_part(
            text=part,
            from_lang=from_lang,
            to_lang=to_lang,
            endpoints=endpoints,
            api_key=api_key,
            api_version=api_version,
            region=region,
        )
        translated_parts.append(translated)
    return "\n".join(item for item in translated_parts if item).strip()


def _translate_single_part(*, text: str, from_lang: str, to_lang: str, endpoints: list[str], api_key: str, api_version: str, region: str) -> str:
    last_error = ""
    for current_endpoint in endpoints:
        # Preferred: Translator preview endpoint (as provided by Azure sample)
        preview_query = parse.urlencode({"api-version": api_version})
        preview_url = f"{current_endpoint.rstrip('/')}/translator/text/translate?{preview_query}"
        preview_payload = json.dumps(
            {
                "inputs": [
                    {
                        "Text": text,
                        "language": from_lang,
                        "targets": [{"language": to_lang}],
                    }
                ]
            }
        ).encode("utf-8")
        preview_headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": api_key,
        }
        try:
            preview_req = request.Request(preview_url, data=preview_payload, headers=preview_headers, method="POST")
            with request.urlopen(preview_req, timeout=60) as resp:
                preview_body = resp.read().decode("utf-8")
            preview_data = _safe_json_loads(preview_body)
            if isinstance(preview_data, dict):
                values = preview_data.get("value")
                if isinstance(values, list) and values and isinstance(values[0], dict):
                    translations = values[0].get("translations", [])
                    if translations:
                        return str(translations[0].get("text", "")).strip()
        except error.HTTPError as exc:  # pragma: no cover - network call
            last_error = exc.read().decode("utf-8", errors="ignore") or exc.reason
            if exc.code not in {404, 405, 401}:
                raise RuntimeError(f"Azure Translator error: {last_error}") from exc

        # Fallback: classic Translator Text API v3.0 endpoint
        classic_query = parse.urlencode({"api-version": "3.0", "from": from_lang, "to": to_lang})
        classic_url = f"{current_endpoint.rstrip('/')}/translate?{classic_query}"
        classic_payload = json.dumps([{"text": text}]).encode("utf-8")
        classic_headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": api_key,
        }
        if region:
            classic_headers["Ocp-Apim-Subscription-Region"] = region

        classic_req = request.Request(classic_url, data=classic_payload, headers=classic_headers, method="POST")
        try:
            with request.urlopen(classic_req, timeout=60) as resp:
                classic_body = resp.read().decode("utf-8")
            classic_data = _safe_json_loads(classic_body)
            if isinstance(classic_data, list) and classic_data and isinstance(classic_data[0], dict):
                translations = classic_data[0].get("translations", [])
                if translations:
                    return str(translations[0].get("text", "")).strip()
        except error.HTTPError as exc:  # pragma: no cover - network call
            last_error = exc.read().decode("utf-8", errors="ignore") or exc.reason
            continue

    raise RuntimeError(f"Azure Translator error: {last_error or 'No translation returned.'}")


def transcribe_with_azure_speech(*, source_path: str = "", source_url: str = "") -> dict[str, str]:
    """Best-effort transcription helper.

    For URL sources it uses Speech REST short transcription endpoint.
    Returns a dict with transcript + detected locale/language code.
    """

    speech_key = _get_setting("AZURE_SPEECH_KEY")
    speech_endpoint = _get_setting("AZURE_SPEECH_ENDPOINT", "https://basic-agent-flow-zak-resource.cognitiveservices.azure.com")
    if not speech_key:
        raise RuntimeError("AZURE_SPEECH_KEY is required for speech transcription.")

    cloud_only = bool(getattr(settings, "CLOUD_TRANSCRIPTION_ONLY", True))
    path = Path(source_path) if source_path else None
    if path and path.exists() and path.suffix.lower() == ".txt":
        return {
            "transcript": path.read_text(encoding="utf-8", errors="replace"),
            "detected_locale": "text/plain",
            "detected_language_code": "en",
        }

    if path and path.exists() and path.suffix.lower() in {".mp4", ".webm", ".mov", ".mkv", ".avi"}:
        if cloud_only:
            raise RuntimeError(
                "CLOUD_TRANSCRIPTION_ONLY is enabled. Provide video_url so Azure can transcribe media in the cloud."
            )
        return _transcribe_local_media_with_sdk(path=path, speech_key=speech_key, speech_endpoint=speech_endpoint)

    if source_url:
        host = (urlparse(source_url).hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "0.0.0.0"}:
            raise RuntimeError(
                "Azure Speech cannot access localhost URLs. Use a publicly reachable HTTPS media URL."
            )
        results: list[dict[str, Any]] = []
        locale_errors: list[str] = []
        locales = _prioritize_locales(source_hint=source_url, locales=_speech_locales())
        for locale in locales:
            try:
                result = _transcribe_url_with_locale(
                    source_url=source_url,
                    speech_key=speech_key,
                    speech_endpoint=speech_endpoint,
                    locale=locale,
                )
                if result.get("transcript"):
                    results.append(result)
            except RuntimeError as exc:
                locale_errors.append(f"{locale}: {exc}")
                continue
        if results:
            ranked = sorted(
                results,
                key=lambda item: (
                    float(item.get("confidence", 0.0)),
                    len(str(item.get("transcript", ""))),
                ),
                reverse=True,
            )
            winner = ranked[0]
            locale = str(winner.get("locale", "en-US"))
            return {
                "transcript": str(winner.get("transcript", "")).strip(),
                "detected_locale": locale,
                "detected_language_code": _language_code_from_locale(locale),
            }
        raise RuntimeError(
            "Azure Speech returned no transcript for all configured locales. "
            + (" | ".join(locale_errors) if locale_errors else "")
        )

    if path and path.exists():
        raise RuntimeError(
            "Local media transcription requires uploading to a public URL or integrating Azure Speech SDK batch upload flow."
        )

    raise RuntimeError("No valid source_path or source_url provided for transcription.")


def _transcribe_local_media_with_sdk(*, path: Path, speech_key: str, speech_endpoint: str) -> dict[str, str]:
    try:
        import azure.cognitiveservices.speech as speechsdk
    except Exception as exc:  # pragma: no cover - dependency-driven
        raise RuntimeError("azure-cognitiveservices-speech package is required for local media transcription.") from exc

    with tempfile.TemporaryDirectory(prefix="lecture-audio-") as tmp_dir:
        audio_path = Path(tmp_dir) / f"{path.stem}.wav"
        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(path),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(audio_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError("ffmpeg is required for local video transcription but is not installed.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ffmpeg audio extraction failed: {exc.stderr.strip()}") from exc

        speech_config = speechsdk.SpeechConfig(subscription=speech_key, endpoint=speech_endpoint)
        speech_config.speech_recognition_language = "en-US"
        audio_config = speechsdk.audio.AudioConfig(filename=str(audio_path))
        recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

        phrases: list[str] = []
        done = {"complete": False}

        def recognized(evt):
            text = getattr(getattr(evt, "result", None), "text", "")
            if text:
                phrases.append(text)

        def stop_cb(evt):
            done["complete"] = True

        recognizer.recognized.connect(recognized)
        recognizer.session_stopped.connect(stop_cb)
        recognizer.canceled.connect(stop_cb)
        recognizer.start_continuous_recognition()

        import time

        timeout_at = time.time() + 60 * 30
        while not done["complete"] and time.time() < timeout_at:
            time.sleep(0.5)
        recognizer.stop_continuous_recognition()

        transcript = " ".join(item.strip() for item in phrases if item.strip()).strip()
        if not transcript:
            raise RuntimeError("Azure Speech SDK returned no transcript for the uploaded video.")
        return {
            "transcript": transcript,
            "detected_locale": speech_config.speech_recognition_language,
            "detected_language_code": _language_code_from_locale(speech_config.speech_recognition_language),
        }


def _transcribe_url_with_locale(*, source_url: str, speech_key: str, speech_endpoint: str, locale: str) -> dict[str, Any]:
    url = f"{speech_endpoint.rstrip('/')}/speechtotext/v3.2/transcriptions"
    payload = json.dumps(
        {
            "contentUrls": [source_url],
            "locale": locale,
            "displayName": "lecture-transcription",
            "properties": {
                "wordLevelTimestampsEnabled": False,
                "profanityFilterMode": "Masked",
                "punctuationMode": "DictatedAndAutomatic",
            },
        }
    ).encode("utf-8")
    req = request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": speech_key,
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8")
            operation_url = resp.headers.get("Location")
    except error.HTTPError as exc:  # pragma: no cover - network call
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Azure Speech error ({locale}): {detail or exc.reason}") from exc
    except error.URLError as exc:  # pragma: no cover - network call
        raise RuntimeError(f"Azure Speech network error ({locale}): {exc.reason}") from exc

    create_data = _safe_json_loads(body) or {}
    if not operation_url:
        operation_url = str(create_data.get("self", "")).strip()
    if not operation_url:
        raise RuntimeError(f"Azure Speech did not return a polling URL for locale {locale}.")

    headers = {"Ocp-Apim-Subscription-Key": speech_key}
    transient_failures = 0
    for _ in range(_speech_poll_attempts()):
        poll_req = request.Request(operation_url, headers=headers, method="GET")
        try:
            with request.urlopen(poll_req, timeout=120) as poll_resp:
                poll_body = poll_resp.read().decode("utf-8")
        except error.URLError:
            transient_failures += 1
            if transient_failures > 4:
                raise RuntimeError(f"Azure Speech polling network instability for locale {locale}.")
            time.sleep(5)
            continue
        poll_data = _safe_json_loads(poll_body) or {}
        status_value = str(poll_data.get("status", "")).lower()
        if status_value == "succeeded":
            files_url = str((poll_data.get("links") or {}).get("files", "")).strip()
            if not files_url:
                raise RuntimeError(f"Azure Speech transcription succeeded but files link is missing for locale {locale}.")
            files_req = request.Request(files_url, headers=headers, method="GET")
            with request.urlopen(files_req, timeout=120) as files_resp:
                files_body = files_resp.read().decode("utf-8")
            files_data = _safe_json_loads(files_body) or {}
            for item in files_data.get("values", []):
                if str(item.get("kind", "")).lower() != "transcription":
                    continue
                content_url = str((item.get("links") or {}).get("contentUrl", "")).strip()
                if not content_url:
                    continue
                content_req = request.Request(content_url, method="GET")
                with request.urlopen(content_req, timeout=120) as content_resp:
                    content_body = content_resp.read().decode("utf-8")
                data = _safe_json_loads(content_body) or {}
                combined = data.get("combinedRecognizedPhrases") or data.get("combinedPhrases") or []
                if combined:
                    transcript = "\n".join(
                        str(item.get("display", "") or item.get("text", "")).strip()
                        for item in combined
                        if item.get("display") or item.get("text")
                    ).strip()
                    return {"transcript": transcript, "locale": locale, "confidence": 0.0}
                return _parse_recognized_phrases(data=data, locale=locale)
            raise RuntimeError(f"Azure Speech transcription output file not found for locale {locale}.")
        if status_value == "failed":
            details = (poll_data.get("properties") or {}).get("error", {})
            detail_text = details.get("message") or details or poll_data
            raise RuntimeError(f"Azure Speech transcription failed ({locale}): {detail_text}")
        time.sleep(5)

    raise RuntimeError(f"Azure Speech transcription timed out for locale {locale}.")


def _parse_recognized_phrases(*, data: dict[str, Any], locale: str) -> dict[str, Any]:
    recognized = data.get("recognizedPhrases") or []
    confidence_values: list[float] = []
    chunks: list[str] = []
    for item in recognized:
        nbest = item.get("nBest") or []
        if not nbest:
            continue
        best = nbest[0]
        display = str(best.get("display", "")).strip()
        if display:
            chunks.append(display)
        confidence = best.get("confidence")
        if isinstance(confidence, (float, int)):
            confidence_values.append(float(confidence))
    transcript = " ".join(chunks).strip()
    average_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
    return {"transcript": transcript, "locale": locale, "confidence": average_confidence}
