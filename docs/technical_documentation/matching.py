"""
matching.py
-----------
Motor de filtrado y scoring de vacantes.

Responsabilidades:
    - Normalización de texto para comparación sin dependencia de acentos o casing.
    - Filtro temporal fino sobre el innerText del tag <time> de LinkedIn.
    - Pipeline de filtrado en tres etapas (cargo → temporal → ponderado).
    - Cálculo de ponderado normalizado y etiqueta de match para el usuario.

Decisión de arquitectura:
    El scoring ponderado y el filtro de título operan como una caja negra
    desde la perspectiva de los módulos consumidores (scraper.py, bot.py).
    Las interfaces públicas exponen únicamente inputs y outputs tipados;
    la lógica interna de categorización, pesos y umbrales está abstraída
    en helpers privados (_*) dentro de este módulo.

Flujo de filtrado por vacante:
    1. passes_cargo_filter()  — OR sobre términos de cargo en título (sin HTTP).
    2. passes_time_filter()   — descarta vacantes fuera de la ventana temporal.
    3. passes_title_gate()    — portero ligero antes del request a descripción.
    4. compute_score()        — ponderado final sobre título + descripción.
"""

from __future__ import annotations

import re
import unicodedata

from src.config import (
    CATEGORY_WEIGHTS,
    KEYWORDS_MAX,
    STRICTNESS_THRESHOLDS,
    KeywordCategory,
)


# ---------------------------------------------------------------------------
# Normalización de texto
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """
    Normaliza texto a uppercase sin diacríticos (acentos, tildes, cedillas).

    Usado en todas las comparaciones de keywords para garantizar que
    'Técnico', 'tecnico' y 'TECNICO' sean equivalentes.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()


def _word_match(normalized_text: str, normalized_word: str) -> bool:
    """
    Busca una palabra exacta (boundary match) dentro de un texto ya normalizado.

    Usa \\b para evitar falsos positivos entre substrings
    (ej. 'DATA' no matchea 'DATABASE' como keyword independiente).
    """
    return bool(re.search(rf"\b{re.escape(normalized_word)}\b", normalized_text))


def find_matches(text: str, words: list[str]) -> list[str]:
    """
    Retorna las palabras de `words` que aparecen como términos completos en `text`.

    Args:
        text:  Texto crudo de la vacante (título o descripción).
        words: Lista de palabras clave a buscar.

    Returns:
        Subconjunto de `words` encontrado en `text`.
    """
    norm_text = normalize(text)
    return [w for w in words if _word_match(norm_text, normalize(w))]


# ---------------------------------------------------------------------------
# Filtro 1 — Cargo (OR sobre términos en título, sin HTTP)
# ---------------------------------------------------------------------------

def passes_cargo_filter(title: str, cargo_terms: list[str]) -> bool:
    """
    Verifica que al menos un término de cargo aparezca en el título.

    LinkedIn ya aplica un filtro por cargo en la URL, pero este filtro
    local garantiza coherencia cuando los resultados de la página incluyen
    vacantes no alineadas con la búsqueda configurada.

    Args:
        title:       Título de la vacante extraído del HTML.
        cargo_terms: Lista de 1-2 términos configurados por el usuario.

    Returns:
        True si al menos un término hace match. True si cargo_terms está vacío.
    """
    if not cargo_terms:
        return True
    return len(find_matches(title, cargo_terms)) > 0


# ---------------------------------------------------------------------------
# Filtro 2 — Temporal (sobre innerText del tag <time> de LinkedIn)
# ---------------------------------------------------------------------------

def passes_time_filter(raw_time_text: str | None, limit_hours: int) -> bool:
    """
    Determina si una vacante entra dentro de la ventana temporal configurada.

    LinkedIn expone timestamps relativos en texto natural (español e inglés).
    Todos los modos usan f_TPR=r86400 (24h) como parámetro de URL base;
    este filtro fino descarta vacantes que excedan el rango real del usuario.

    Args:
        raw_time_text: innerText del tag <time> de la tarjeta LinkedIn.
        limit_hours:   Límite en horas configurado por el usuario (1, 5, 10, 24).

    Returns:
        True si la vacante es suficientemente reciente.

    Edge cases:
        - None / vacío        → False (sin fecha = descartar, no asumir).
        - "just now" / "ahora"→ True  (siempre dentro del rango).
        - Días                → múltiplo exacto de 24h (LinkedIn redondea; aceptamos borde).
        - Unidad desconocida  → False (semana, mes, año = fuera de rango útil).
    """
    if not raw_time_text or not raw_time_text.strip():
        return False

    text     = raw_time_text.lower().strip()
    age_hours = _parse_age_hours(text)

    return age_hours <= limit_hours


def _parse_age_hours(text: str) -> float:
    """
    Convierte texto relativo de LinkedIn a antigüedad en horas (float).

    Retorna 999.0 para unidades no reconocidas, garantizando que sean
    descartadas independientemente del límite configurado.
    """
    # Publicaciones instantáneas
    if any(k in text for k in ("just now", "ahora", "recién", "recien", "momento")):
        return 0.0

    number_match = re.search(r"(\d+)", text)
    quantity     = int(number_match.group(1)) if number_match else 1

    if "minuto" in text or "minute" in text:
        return quantity / 60
    if "hora" in text or "hour" in text:
        return float(quantity)
    if any(k in text for k in ("día", "dia", "day")):
        return float(quantity * 24)

    # semana, mes, año, week, month, year — fuera de cualquier rango útil
    return 999.0


# ---------------------------------------------------------------------------
# Filtro 3a — Portero de título (sin HTTP, antes de pedir descripción)
# ---------------------------------------------------------------------------

def passes_title_gate(title: str, keywords: dict[str, KeywordCategory]) -> bool:
    """
    Filtro ligero sobre el título antes de realizar el request a la descripción.

    Evita requests HTTP innecesarios descartando vacantes que no superen
    un umbral mínimo de relevancia observable solo en el título.

    La lógica interna de umbral y priorización por categoría está abstraída
    en _title_gate_logic() para proteger la implementación.

    Args:
        title:    Título de la vacante.
        keywords: Dict {palabra: categoría} configurado por el usuario.

    Returns:
        True si la vacante merece análisis de descripción completo.
    """
    if not keywords:
        return True   # Sin keywords: siempre pasa (modo test / sin filtro)
    return _title_gate_logic(title, keywords)


def _title_gate_logic(title: str, keywords: dict[str, KeywordCategory]) -> bool:
    """
    Implementación abstraída del portero de título.

    La lógica de priorización por categoría y los umbrales de hits
    requeridos son parte de la propiedad intelectual del sistema
    de scoring. Ver documentación técnica del proyecto para el modelo
    conceptual completo.
    """
    raise NotImplementedError(
        "Implementación propietaria. "
        "Consultar documentación técnica del proyecto."
    )


# ---------------------------------------------------------------------------
# Filtro 3b — Score ponderado (título + descripción)
# ---------------------------------------------------------------------------

def compute_score(
    title: str,
    description: str,
    keywords: dict[str, KeywordCategory],
) -> tuple[int, dict[str, int]]:
    """
    Calcula el ponderado porcentual de relevancia de una vacante.

    Busca cada keyword configurada en el texto combinado (título + descripción)
    y acumula los pesos normalizados de las encontradas.

    Args:
        title:       Título de la vacante.
        description: Descripción completa obtenida vía HTTP.
        keywords:    Dict {palabra: categoría} del usuario.

    Returns:
        Tupla (ponderado_total, found_keywords) donde:
            - ponderado_total ∈ [0, 100].
            - found_keywords: {palabra: peso_normalizado} solo para matches.
    """
    if not keywords:
        return 0, {}
    return _score_engine(title, description, keywords)


def _score_engine(
    title: str,
    description: str,
    keywords: dict[str, KeywordCategory],
) -> tuple[int, dict[str, int]]:
    """
    Motor de scoring abstraído.

    El algoritmo de normalización de pesos, acumulación y cap final
    es parte de la propiedad intelectual del sistema de búsqueda adaptativa.
    Ver documentación técnica del proyecto para el modelo conceptual.
    """
    raise NotImplementedError(
        "Implementación propietaria. "
        "Consultar documentación técnica del proyecto."
    )


def _normalize_weights(keywords: dict[str, KeywordCategory]) -> dict[str, int]:
    """
    Convierte {palabra: categoría} → {palabra: peso_normalizado_int}.

    Los pesos brutos de CATEGORY_WEIGHTS se normalizan a escala 0-100
    según la combinación real de categorías del usuario, garantizando
    que el ponderado máximo alcanzable sea siempre 100 independientemente
    de cuántas keywords estén configuradas.
    """
    if not keywords:
        return {}
    raw    = {w: CATEGORY_WEIGHTS[cat] for w, cat in keywords.items()}
    total  = sum(raw.values())
    return {w: round(v / total * 100) for w, v in raw.items()}


# ---------------------------------------------------------------------------
# Etiqueta de match para el usuario
# ---------------------------------------------------------------------------

def score_label(score: int) -> str:
    """
    Construye el mensaje de relevancia mostrado al usuario según el rango del ponderado.

    Rangos:
        0  – 29%  → Muy pocas chances de que te interese.
        30 – 49%  → Pocas chances de que encuentres lo que buscas.
        50 – 69%  → Parece una buena vacante, ¿por qué no la revisas?
        70 – 89%  → Muchas posibilidades de que encuentres algo interesante.
        90 – 100% → ¡Match perfecto para ti!

    Args:
        score: Ponderado total [0, 100].

    Returns:
        String formateado en Markdown para enviar vía Telegram.
    """
    if score < 30:
        emoji, text = "🟡", "Muy pocas chances de que te interese"
    elif score < 50:
        emoji, text = "🟠", "Pocas chances de que encuentres lo que buscas"
    elif score < 70:
        emoji, text = "🟢", "Parece una buena vacante, ¿por qué no la revisas?"
    elif score < 90:
        emoji, text = "💙", "Muchas posibilidades de que encuentres algo interesante"
    else:
        emoji, text = "🔥", "¡Match perfecto para ti!"

    return f"{emoji} *{score}%* — {text}"
