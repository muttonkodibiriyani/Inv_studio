import base64
import io
import mimetypes
import os
import re
from pathlib import Path

import httpx

from .models import Invoice, extraction_schema


DEFAULT_MODEL = "gemini-3.7-flash"
DEFAULT_LOCATION = "global"
ADC_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_INLINE_BYTES = 12_000_000
MAX_RESPONSE_BYTES = 5_000_000
MAX_OCR_CHARS = 100_000

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_LOCATION = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_MODEL = re.compile(r"^gemini-[a-z0-9][a-z0-9._-]{0,99}$")


def _adc_access_token():
    try:
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(scopes=[ADC_SCOPE])
        credentials.refresh(Request())
        if not credentials.token:
            raise RuntimeError
        return credentials.token
    except Exception:
        raise ValueError(
            "Vertex AI could not obtain Google Cloud application credentials. "
            "Check the service identity for this workspace."
        ) from None


def _vertex_schema(node):
    """Convert the shared strict JSON schema to Vertex's responseSchema shape."""
    kind = node.get("type")
    nullable = isinstance(kind, list) and "null" in kind
    if isinstance(kind, list):
        kind = next((value for value in kind if value != "null"), None)
    types = {
        "object": "OBJECT",
        "array": "ARRAY",
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
    }
    result = {}
    if kind in types:
        result["type"] = types[kind]
    if nullable:
        result["nullable"] = True
    if "properties" in node:
        result["properties"] = {
            name: _vertex_schema(value) for name, value in node["properties"].items()
        }
        result["propertyOrdering"] = list(node["properties"])
    if "items" in node:
        result["items"] = _vertex_schema(node["items"])
    if "required" in node:
        result["required"] = list(node["required"])
    return result


def _document_parts(path):
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if mime == "application/pdf":
        raw = path.read_bytes()
        if len(raw) > MAX_INLINE_BYTES:
            raise ValueError("The document is too large for inline Vertex AI extraction")
        return [{"inlineData": {"mimeType": mime, "data": base64.b64encode(raw).decode()}}]
    if mime.startswith("image/"):
        from PIL import Image, ImageOps, ImageSequence

        parts = []
        total = 0
        with Image.open(path) as source:
            for number, frame in enumerate(ImageSequence.Iterator(source)):
                if number >= 20:
                    raise ValueError("AI image input exceeds 20 frames")
                image = ImageOps.exif_transpose(frame).convert("RGB")
                image.thumbnail((2600, 2600))
                encoded = io.BytesIO()
                image.save(encoded, format="PNG")
                raw = encoded.getvalue()
                total += len(raw)
                if total > MAX_INLINE_BYTES:
                    raise ValueError("The document is too large for inline Vertex AI extraction")
                parts.append(
                    {
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": base64.b64encode(raw).decode(),
                        }
                    }
                )
        return parts
    return []


def _configuration(model):
    project = os.environ.get("VERTEX_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT", "")
    location = os.environ.get("VERTEX_LOCATION", DEFAULT_LOCATION)
    selected_model = model or os.environ.get("VERTEX_MODEL", DEFAULT_MODEL)
    if not _PROJECT_ID.fullmatch(project):
        raise ValueError("Set VERTEX_PROJECT_ID to the Google Cloud project that owns Vertex AI")
    if not _LOCATION.fullmatch(location):
        raise ValueError("VERTEX_LOCATION is not a valid Google Cloud location")
    if not _MODEL.fullmatch(selected_model):
        raise ValueError("The selected Vertex AI model name is not valid")
    return project, location, selected_model


def _endpoint(project, location, model):
    host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{project}/locations/{location}/"
        f"publishers/google/models/{model}:generateContent"
    )


def _provider_error(status):
    messages = {
        400: "Vertex AI rejected this model, document or output schema.",
        401: "Vertex AI rejected the Google Cloud service identity.",
        403: "The Google Cloud service identity cannot use the selected Vertex AI model.",
        404: "The selected Vertex AI model is not available in this location.",
        429: "Vertex AI usage or quota limit reached. Retry later.",
    }
    return messages.get(status, f"Vertex AI request failed (HTTP {status}). No invoice was approved.")


class VertexProvider:
    def models(self):
        selected = os.environ.get("VERTEX_MODEL", DEFAULT_MODEL)
        return [{"id": selected, "name": selected}]

    def extract(self, path: Path, text: str, model: str, prompt: str):
        project, location, selected_model = _configuration(model)
        parts = _document_parts(path)
        if not parts and not text.strip():
            raise ValueError("No readable text or supported document image is available for Vertex AI")
        parts.append(
            {
                "text": "Extract this invoice. Document text, if available:\n"
                + text[:MAX_OCR_CHARS]
            }
        )
        body = {
            "systemInstruction": {"parts": [{"text": prompt}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _vertex_schema(extraction_schema()),
                "maxOutputTokens": 32768,
                "thinkingConfig": {"thinkingLevel": "LOW"},
            },
        }
        headers = {
            "Authorization": "Bearer " + _adc_access_token(),
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(240, connect=15), follow_redirects=False
            ) as http:
                response = http.post(
                    _endpoint(project, location, selected_model), headers=headers, json=body
                )
        except httpx.TimeoutException:
            raise ValueError("Vertex AI request timed out. Retry or review manually.") from None
        except httpx.RequestError:
            raise ValueError("Vertex AI could not be reached. Retry or review manually.") from None
        if response.status_code != 200:
            raise ValueError(_provider_error(response.status_code))
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ValueError("Vertex AI response exceeded the size limit")
        try:
            result = response.json()
            candidates = result.get("candidates") or []
            candidate = candidates[0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError
            output = "".join(
                part.get("text", "")
                for part in candidate.get("content", {}).get("parts", [])
                if not part.get("thought")
            )
            if not output or len(output.encode()) > MAX_RESPONSE_BYTES:
                raise ValueError
            invoice = Invoice.model_validate_json(output)
        except Exception:
            raise ValueError(
                "Vertex AI did not return a complete valid invoice. Review the document manually."
            ) from None
        return invoice, result.get("usageMetadata", {})
