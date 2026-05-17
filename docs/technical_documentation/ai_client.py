"""
ai_client.py
------------
Cliente de integración con la API de Gemini (Google Generative Language).

Responsabilidades:
    - Modo consejero: análisis de perfil profesional con sugerencias estratégicas,
      cargos recomendados y keywords optimizadas para LinkedIn.
    - Modo wizard: configuración estructurada de búsqueda a partir de descripción
      en lenguaje natural. Retorna JSON validado y aplicable directamente.

Decisión de arquitectura:
    Los system prompts están separados por modo para mantener comportamiento
    predecible y tokens mínimos por llamada. El modo wizard usa temperatura
    baja (0.3) para maximizar consistencia del JSON de salida. El modo
    consejero usa temperatura media (0.7) para respuestas más naturales.

    Ambos modos son stateless: no mantienen historial de conversación entre
    llamadas. El contexto completo se pasa en cada request.

Flujo de llamada:
    Módulos externos → call_advisor() | call_wizard()
                     → _call_gemini() (HTTP genérico)
                     → Validación / parseo
                     → Retorno tipado
"""

from __future__ import annotations

import json
import logging

import requests

from src.config import (
    AI_MAX_CHARS,
    GEMINI_API_KEY,
    GEMINI_URL,
    KEYWORDS_MAX,
    KEYWORDS_MIN,
    STRICTNESS_THRESHOLDS,
    KeywordCategory,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PROMPT_ADVISOR = (
    "Eres un experto en orientacion laboral y busqueda de empleo en Latinoamerica. "
    "Analiza el perfil profesional y objetivos del usuario. "
    "Responde SIEMPRE en espanol con tres secciones:\n\n"
    "1. CONSEJOS ESTRATEGICOS: 3-5 consejos concretos y accionables.\n"
    "2. CARGOS SUGERIDOS: 5-8 cargos especificos que se ajusten al perfil.\n"
    "3. PALABRAS CLAVE: exactamente 15 keywords optimizadas para LinkedIn, "
    "sin tildes, en espanol e ingles segun aplique.\n\n"
    "Se directo, practico y enfocado en el mercado colombiano/latinoamericano."
)

_PROMPT_WIZARD = (
    "Eres un asistente de configuracion de busqueda de empleo en Colombia y Latinoamerica. "
    "El usuario describe en lenguaje natural que trabajo busca. "
    "Tu tarea es extraer una configuracion de busqueda estructurada.\n\n"
    "Devuelve UNICAMENTE un objeto JSON valido, sin texto adicional, sin bloques de codigo, "
    "sin explicaciones. El JSON debe tener exactamente esta estructura:\n"
    "{\n"
    '  "cargo": ["termino1"],\n'
    '  "keywords": {\n'
    '    "palabra1": "esencial",\n'
    '    "palabra2": "importante",\n'
    '    "palabra3": "bonus",\n'
    '    "palabra4": "bonus"\n'
    "  },\n"
    '  "exigencia": "balanceado"\n'
    "}\n\n"
    "Reglas:\n"
    "- cargo: 1 o 2 terminos cortos que alguien escribiria en LinkedIn (sin tildes).\n"
    "- keywords: entre 2 y 4 entradas. "
    "Categorias: esencial (max 1), importante (max 1), bonus (sin limite hasta 4 total).\n"
    "- Sin tildes en ningun valor de texto.\n"
    "- exigencia: una de estas tres opciones exactas: flexible, balanceado, estricto.\n"
    "- Si el perfil es vago, infiere lo mas razonable para Colombia.\n"
    "- No incluyas nada fuera del JSON."
)


# ---------------------------------------------------------------------------
# Cliente HTTP genérico
# ---------------------------------------------------------------------------

def _call_gemini(
    system_prompt: str,
    user_text: str,
    temperature: float = 0.7,
    max_tokens: int = 1500,
) -> str | None:
    """
    Realiza una llamada HTTP a la API de Gemini y retorna el texto de respuesta.

    Args:
        system_prompt: Instrucción de sistema que define el comportamiento del modelo.
        user_text:     Mensaje del usuario a procesar.
        temperature:   Temperatura de generación (0.0 - 1.0).
        max_tokens:    Límite de tokens en la respuesta.

    Returns:
        Texto de respuesta del modelo. None si la llamada falla.
    """
    if not GEMINI_API_KEY:
        log.error("GEMINI_API_KEY no configurada.")
        return None

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents":           [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature":     temperature,
            "maxOutputTokens": max_tokens,
        },
    }

    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]

    except requests.Timeout:
        log.warning("Gemini timeout.")
        return None
    except Exception as e:
        log.error(f"Gemini error: {e}")
        return None


# ---------------------------------------------------------------------------
# Modo consejero
# ---------------------------------------------------------------------------

def call_advisor(profile_text: str) -> str:
    """
    Analiza el perfil profesional del usuario y retorna sugerencias estratégicas.

    Incluye: consejos de búsqueda, cargos recomendados y 15 keywords para LinkedIn.

    Args:
        profile_text: Descripción libre del perfil y objetivos del usuario.
                      Máximo AI_MAX_CHARS caracteres (validado por el caller).

    Returns:
        Respuesta formateada en español lista para enviar al usuario.
        Mensaje de error descriptivo si la llamada falla.
    """
    if not profile_text or not profile_text.strip():
        return "⚠️ El perfil no puede estar vacío."

    result = _call_gemini(
        system_prompt=_PROMPT_ADVISOR,
        user_text=profile_text,
        temperature=0.7,
        max_tokens=1500,
    )

    if result is None:
        return "❌ Error al conectar con la IA. Intenta más tarde."

    return result


# ---------------------------------------------------------------------------
# Modo wizard
# ---------------------------------------------------------------------------

class WizardConfig:
    """
    Configuración estructurada generada por el modo wizard de la IA.

    Attributes:
        cargo:     Lista de 1-2 términos de cargo.
        keywords:  Dict {palabra: categoría} con 2-4 entradas validadas.
        exigencia: Nivel de exigencia validado contra STRICTNESS_THRESHOLDS.
        ponderado_min: Umbral numérico derivado de exigencia.
    """

    def __init__(
        self,
        cargo: list[str],
        keywords: dict[str, KeywordCategory],
        exigencia: str,
    ) -> None:
        self.cargo:         list[str]                    = cargo
        self.keywords:      dict[str, KeywordCategory]   = keywords
        self.exigencia:     str                          = exigencia
        self.ponderado_min: int                          = STRICTNESS_THRESHOLDS.get(exigencia, 60)


def call_wizard(profile_text: str) -> WizardConfig | None:
    """
    Genera una configuración de búsqueda estructurada a partir de lenguaje natural.

    Llama a Gemini con temperatura baja para maximizar consistencia del JSON.
    Valida y sanitiza la respuesta antes de construir el WizardConfig.

    Args:
        profile_text: Descripción libre del tipo de trabajo buscado.

    Returns:
        WizardConfig validado y listo para aplicar. None si la IA falla o
        la respuesta no supera la validación mínima.
    """
    raw = _call_gemini(
        system_prompt=_PROMPT_WIZARD,
        user_text=profile_text,
        temperature=0.3,
        max_tokens=400,
    )

    if raw is None:
        return None

    return _parse_wizard_response(raw)


def _parse_wizard_response(raw: str) -> WizardConfig | None:
    """
    Parsea y valida la respuesta JSON del modo wizard.

    Sanitiza markdown fences que Gemini ocasionalmente incluye a pesar
    del prompt. Aplica límites de categorías (max 1 esencial, max 1 importante)
    y recorta al máximo de keywords permitido.

    Args:
        raw: Texto crudo retornado por la API de Gemini.

    Returns:
        WizardConfig validado. None si el JSON es inválido o incompleto.
    """
    try:
        cleaned = raw.strip().strip("```json").strip("```").strip()
        data    = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        log.error(f"Wizard JSON parse error: {e} | raw: {raw[:200]}")
        return None

    cargo     = data.get("cargo", [])
    keywords  = data.get("keywords", {})
    exigencia = data.get("exigencia", "balanceado")

    # Validación mínima
    if not cargo or not keywords or len(keywords) < KEYWORDS_MIN:
        log.warning(f"Wizard config incompleta: cargo={cargo}, kw_count={len(keywords)}")
        return None

    # Sanitización de cargo
    cargo_clean = [c.strip() for c in cargo if c.strip()][:2]

    # Sanitización de keywords: respetar límites de categoría
    valid_kw = {
        k.strip(): v
        for k, v in keywords.items()
        if v in ("esencial", "importante", "bonus") and k.strip()
    }
    kw_final = _enforce_category_limits(valid_kw)

    # Validar exigencia
    if exigencia not in STRICTNESS_THRESHOLDS:
        exigencia = "balanceado"

    return WizardConfig(
        cargo=cargo_clean,
        keywords=kw_final,
        exigencia=exigencia,
    )


def _enforce_category_limits(
    keywords: dict[str, KeywordCategory],
) -> dict[str, KeywordCategory]:
    """
    Aplica los límites de categoría y recorta al máximo de keywords permitido.

    Reglas:
        - Máximo 1 keyword de categoría 'esencial'.
        - Máximo 1 keyword de categoría 'importante'.
        - Sin límite para 'bonus' dentro del máximo total.
        - Máximo total: KEYWORDS_MAX entradas.

    Args:
        keywords: Dict crudo {palabra: categoría} sin validar límites.

    Returns:
        Dict sanitizado respetando todos los límites.
    """
    esenciales  = [(k, v) for k, v in keywords.items() if v == "esencial"]
    importantes = [(k, v) for k, v in keywords.items() if v == "importante"]
    bonus       = [(k, v) for k, v in keywords.items() if v == "bonus"]

    combined = (
        esenciales[:1]
        + importantes[:1]
        + bonus
    )

    return dict(list(dict(combined).items())[:KEYWORDS_MAX])
