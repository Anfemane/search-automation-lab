"""
scraper.py
----------
Motor de extracción de vacantes desde LinkedIn (Colombia).

Responsabilidades:
    - Construcción de URLs de búsqueda parametrizadas por configuración de usuario.
    - Extracción de datos de tarjetas de vacante desde HTML de LinkedIn.
    - Obtención de descripción completa vía request individual por vacante.
    - Pipeline de scraping con tres mecanismos de control de flujo abstraídos:
        1. Early stop adaptativo  — detiene el barrido cuando no hay resultados nuevos.
        2. Hard stop universal    — límite absoluto de vacantes analizadas por barrido.
        3. Cooldown post hard stop — restricción temporal en disco tras alcanzar el límite.

Decisión de arquitectura:
    La lógica de comportamiento evasivo (jitter, timing adaptativo, umbrales
    de early stop, patrones de interrupción no determinísticos) está completamente
    abstraída en helpers privados (_*). Los módulos consumidores (bot.py) interactúan
    únicamente con run_sweep() y sus tipos de retorno.

    Ver documentación técnica del proyecto para el modelo conceptual completo
    del sistema de scraping evasivo y su fundamento en análisis de publicación
    de vacantes en Colombia.
"""

from __future__ import annotations

import logging
import random
import re
import time

import requests
from bs4 import BeautifulSoup

from src.config import (
    FULL_SWEEP_PAGES,
    HARD_STOP_LIMIT,
    MAX_429_STREAK,
    MAX_RETRIES,
    TIME_RANGE_HOURS,
    TIME_RANGE_URL_PARAMS,
    USER_AGENTS,
    UserSearchConfig,
)
from src.matching import (
    compute_score,
    find_matches,
    passes_cargo_filter,
    passes_time_filter,
    passes_title_gate,
)
from src.storage import (
    activate_cooldown,
    append_to_history,
    load_history,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tipos de retorno
# ---------------------------------------------------------------------------

class SweepResult:
    """
    Resultado de un barrido completo.

    Attributes:
        analyzed:     Total de vacantes inspeccionadas en el barrido.
        discarded:    Vacantes descartadas por cualquier filtro o deduplicación.
        matched:      Vacantes que superaron todos los filtros y fueron enviadas.
        hard_stopped: True si el barrido fue interrumpido por el hard stop.
        cooldown_seconds: Duración del cooldown activado (0 si no hubo hard stop).
    """

    def __init__(self) -> None:
        self.analyzed:          int  = 0
        self.discarded:         int  = 0
        self.matched:           int  = 0
        self.hard_stopped:      bool = False
        self.cooldown_seconds:  int  = 0


class JobCard:
    """
    Datos extraídos de una tarjeta de vacante de LinkedIn.

    Attributes:
        job_id:    Identificador normalizado (extraído del path de la URL).
        title:     Título del puesto.
        company:   Nombre de la empresa.
        link:      URL limpia sin parámetros de tracking.
        date_display: Fecha formateada para mostrar al usuario.
        date_raw:     innerText del tag <time> para filtro temporal.
        score:        Ponderado calculado (0-100). 0 en modo sin_filtro.
        found_kw:     Keywords encontradas con sus pesos. Vacío en modo sin_filtro.
    """

    def __init__(self) -> None:
        self.job_id:       str              = ""
        self.title:        str              = ""
        self.company:      str              = "N/A"
        self.link:         str              = ""
        self.date_display: str              = "—"
        self.date_raw:     str | None       = None
        self.score:        int              = 0
        self.found_kw:     dict[str, int]   = {}


# ---------------------------------------------------------------------------
# Construcción de URL
# ---------------------------------------------------------------------------

def build_url(cfg: UserSearchConfig, page_offset: int) -> str:
    """
    Construye la URL de búsqueda de LinkedIn para una página específica.

    Todos los modos con filtro de tiempo usan f_TPR=r86400 (24h) como base.
    El filtro temporal fino se aplica en matching.py sobre el innerText del <time>.

    Args:
        cfg:         Configuración activa del usuario.
        page_offset: Offset de paginación (múltiplo de 25).

    Returns:
        URL completa lista para el request HTTP.
    """
    base     = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?location=Colombia"
    kw_param = _build_keyword_param(cfg.cargo)
    time_param = TIME_RANGE_URL_PARAMS.get(cfg.tiempo, "f_TPR=r86400")

    if cfg.es_relevancia:
        return f"{base}{kw_param}&sortBy=R&start={page_offset}"

    return f"{base}{kw_param}&{time_param}&start={page_offset}"


def _build_keyword_param(cargo_terms: list[str]) -> str:
    """Construye el parámetro &keywords= a partir de los términos de cargo."""
    if not cargo_terms:
        return ""
    from src.matching import normalize
    kw = "+".join(normalize(t).replace(" ", "+") for t in cargo_terms)
    return f"&keywords={kw}"


# ---------------------------------------------------------------------------
# Extracción de datos de tarjeta HTML
# ---------------------------------------------------------------------------

def extract_card_data(card_element) -> JobCard:
    """
    Extrae los datos de una tarjeta de vacante del HTML de LinkedIn.

    Args:
        card_element: Elemento BeautifulSoup correspondiente a una tarjeta.

    Returns:
        JobCard poblado con los datos disponibles en la tarjeta.
        Campos no encontrados mantienen sus valores por defecto.
    """
    card = JobCard()

    tag_a = card_element.find("a")
    tag_title = card_element.find(
        ["h3", "h4", "span"],
        class_=lambda x: x and "title" in x.lower(),
    )

    if tag_a:
        link_raw   = tag_a.get("href", "")
        card.link  = link_raw.split("?")[0]
        card.job_id = _normalize_job_id(link_raw)

    if tag_title:
        card.title = tag_title.text.strip()

    # Empresa
    tag_company = card_element.find(
        ["h4", "a", "span"],
        class_=lambda x: x and any(
            k in x.lower() for k in ["company", "subtitle", "empresa"]
        ),
    )
    if tag_company:
        card.company = tag_company.text.strip()

    # Fecha
    tag_date = card_element.find(
        ["time", "span"],
        class_=lambda x: x and any(
            k in x.lower() for k in ["date", "time", "listed", "posted"]
        ),
    )
    if tag_date:
        card.date_display = tag_date.get("datetime") or tag_date.text.strip()
        card.date_raw     = tag_date.text.strip()

    return card


def _normalize_job_id(link: str) -> str:
    """
    Extrae el ID numérico de una URL de vacante de LinkedIn.

    Formato esperado: /jobs/view/<slug>-<id>
    Fallback: URL sin parámetros de query.
    """
    match = re.search(r"/jobs/view/[^/]+-(\d+)", link)
    return match.group(1) if match else link.split("?")[0].rstrip("/")


# ---------------------------------------------------------------------------
# Obtención de descripción completa
# ---------------------------------------------------------------------------

def fetch_description(link: str, headers: dict) -> str:
    """
    Realiza un request HTTP a la página de la vacante y extrae la descripción.

    Incluye delay aleatorio antes del request para comportamiento no determinístico.

    Args:
        link:    URL limpia de la vacante.
        headers: Headers del request incluyendo User-Agent rotado.

    Returns:
        Texto plano de la descripción. String vacío si falla o no se encuentra.
    """
    try:
        time.sleep(random.uniform(1.0, 2.5))
        res = requests.get(link, headers=headers, timeout=15)
        if res.status_code != 200:
            return ""
        soup = BeautifulSoup(res.text, "html.parser")
        container = soup.find(
            "div", class_=lambda x: x and "description" in x.lower()
        )
        if not container:
            return ""
        return re.sub(r"\s+", " ", container.get_text(separator=" ", strip=True))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Control de flujo del barrido — lógica abstraída
# ---------------------------------------------------------------------------

def _get_scan_strategy() -> dict:
    """
    Genera los parámetros de comportamiento evasivo para un barrido.

    Retorna un dict con los umbrales y configuración de timing para
    early stop, jitter y patrones de interrupción. Los valores se
    calculan una vez por barrido para que el patrón varíe entre ejecuciones.

    La lógica de cálculo de estos parámetros es parte de la propiedad
    intelectual del sistema adaptativo. Ver documentación técnica del proyecto.
    """
    raise NotImplementedError(
        "Implementación propietaria. "
        "Consultar documentación técnica del proyecto."
    )


def _should_early_stop(
    consecutive_empty_pages: int,
    strategy: dict,
) -> bool:
    """
    Evalúa si el barrido debe detenerse anticipadamente.

    El umbral de páginas consecutivas sin resultados de cargo varía
    por barrido según la estrategia generada en _get_scan_strategy(),
    produciendo patrones de interrupción no determinísticos.
    """
    raise NotImplementedError(
        "Implementación propietaria. "
        "Consultar documentación técnica del proyecto."
    )


def _inter_page_delay(strategy: dict) -> None:
    """
    Aplica un delay entre páginas con jitter aleatorio.

    El rango y distribución del delay son parte del modelo de
    comportamiento evasivo. Ver documentación técnica del proyecto.
    """
    raise NotImplementedError(
        "Implementación propietaria. "
        "Consultar documentación técnica del proyecto."
    )


# ---------------------------------------------------------------------------
# Motor principal de barrido
# ---------------------------------------------------------------------------

def run_sweep(
    uid: int,
    cfg: UserSearchConfig,
    no_filter: bool = False,
    limit: int | None = None,
    progress_callback=None,
    match_callback=None,
) -> SweepResult:
    """
    Ejecuta un barrido completo de vacantes para un usuario.

    Pipeline por vacante:
        1. Deduplicación por historial (O(1)).
        2. Filtro de cargo en título (sin HTTP).
        3. Filtro temporal fino sobre innerText del <time>.
        4. Portero de título (sin HTTP, antes de pedir descripción).
        5. Request a descripción + cálculo de ponderado.
        6. Invocación de match_callback con JobCard poblado.

    Mecanismos de control de flujo:
        - Early stop adaptativo: para cuando N páginas consecutivas no
          tienen resultados de cargo. N varía por barrido (no determinístico).
        - Hard stop universal: corta al alcanzar HARD_STOP_LIMIT vacantes
          analizadas y activa cooldown en disco.
        - 429 streak: aborta el barrido tras MAX_429_STREAK errores seguidos.

    Args:
        uid:               ID del usuario.
        cfg:               Configuración activa de búsqueda.
        no_filter:         Si True, desactiva todos los filtros (modo test admin).
        limit:             Máximo de matches a enviar (modo test admin).
        progress_callback: Callable(page_index, total_pages) para notificar progreso.
        match_callback:    Callable(uid, card) invocado por cada vacante que supera
                           los filtros. Debe retornar el message_id enviado o None.

    Returns:
        SweepResult con las métricas del barrido.
    """
    result   = SweepResult()
    history  = load_history(uid)
    seen_now: set[str] = set()

    pages        = list(FULL_SWEEP_PAGES)
    total_pages  = len(pages)
    streak_429   = 0

    # Límite de horas para filtro temporal (None en modo relevancia y no_filter)
    limit_hours = TIME_RANGE_HOURS.get(cfg.tiempo) if not cfg.es_relevancia else None

    strategy            = _get_scan_strategy()
    consecutive_empty   = 0

    for idx, page_offset in enumerate(pages):

        if progress_callback and idx > 0 and idx % 5 == 0:
            progress_callback(idx + 1, total_pages)

        url     = build_url(cfg, page_offset)
        headers = {"User-Agent": random.choice(USER_AGENTS)}

        page_result = _fetch_page(url, headers, streak_429)

        if page_result is None:
            # Página vacía — posible fin de resultados
            log.info(f"[{uid}] Página {page_offset} vacía. Deteniendo barrido.")
            break

        if page_result == "429":
            streak_429 += 1
            if streak_429 >= MAX_429_STREAK:
                log.warning(f"[{uid}] Hard 429 streak. Abortando barrido.")
                break
            time.sleep(60)
            continue

        streak_429 = 0
        cards, hits_cargo = _process_page(
            uid         = uid,
            card_elements = page_result,
            cfg         = cfg,
            history     = history,
            seen_now    = seen_now,
            no_filter   = no_filter,
            limit_hours = limit_hours,
            result      = result,
            limit       = limit,
            match_callback = match_callback,
            headers     = headers,
        )

        # Hard stop
        if result.hard_stopped:
            result.cooldown_seconds = activate_cooldown(uid)
            log.info(f"[{uid}] Hard stop. Cooldown: {result.cooldown_seconds}s.")
            break

        # Límite de test
        if limit and result.matched >= limit:
            break

        # Early stop
        if not no_filter and cfg.cargo:
            if hits_cargo == 0:
                consecutive_empty += 1
                if _should_early_stop(consecutive_empty, strategy):
                    log.info(f"[{uid}] Early stop tras {consecutive_empty} páginas sin cargo.")
                    break
            else:
                consecutive_empty = 0

        _inter_page_delay(strategy)

    return result


# ---------------------------------------------------------------------------
# Helpers internos del motor
# ---------------------------------------------------------------------------

def _fetch_page(url: str, headers: dict, streak_429: int):
    """
    Realiza el request HTTP a una página de resultados de LinkedIn.

    Returns:
        Lista de elementos BeautifulSoup (tarjetas de vacante) si exitoso.
        "429" si la respuesta fue un rate limit.
        None si la página está vacía o falló tras los reintentos.
    """
    for attempt in range(MAX_RETRIES + 1):
        try:
            res = requests.get(url, headers=headers, timeout=15)

            if res.status_code == 429:
                return "429"

            if res.status_code != 200:
                if attempt < MAX_RETRIES:
                    time.sleep(5)
                    continue
                return None

            soup  = BeautifulSoup(res.text, "html.parser")
            cards = soup.find_all(
                ["li", "div"],
                class_=lambda x: x and "job-search-card" in x,
            )
            return cards if cards else None

        except requests.RequestException as e:
            log.error(f"Request error (attempt {attempt}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(5)

    return None


def _process_page(
    uid: int,
    card_elements: list,
    cfg: UserSearchConfig,
    history: set[str],
    seen_now: set[str],
    no_filter: bool,
    limit_hours: int | None,
    result: SweepResult,
    limit: int | None,
    match_callback,
    headers: dict,
) -> tuple[list[JobCard], int]:
    """
    Procesa todas las tarjetas de una página aplicando el pipeline de filtrado.

    Returns:
        Tupla (cards_procesadas, hits_cargo_en_pagina).
    """
    processed:  list[JobCard] = []
    hits_cargo: int           = 0

    for element in card_elements:

        if result.analyzed >= HARD_STOP_LIMIT:
            result.hard_stopped = True
            break

        if limit and result.matched >= limit:
            break

        try:
            result.analyzed += 1
            card = extract_card_data(element)

            if not card.job_id or not card.title or not card.link:
                continue

            # Deduplicación
            if card.job_id in history or card.job_id in seen_now:
                result.discarded += 1
                continue
            seen_now.add(card.job_id)

            if not no_filter:
                # Filtro 1: cargo
                if cfg.cargo and not passes_cargo_filter(card.title, cfg.cargo):
                    result.discarded += 1
                    continue
                hits_cargo += 1

                # Filtro 2: temporal
                if limit_hours is not None:
                    if not passes_time_filter(card.date_raw, limit_hours):
                        result.discarded += 1
                        continue

                # Filtro 3a: portero de título
                if cfg.keywords and not passes_title_gate(card.title, cfg.keywords):
                    result.discarded += 1
                    continue

                # Filtro 3b: ponderado (requiere descripción)
                if cfg.keywords:
                    desc = fetch_description(card.link, headers)
                    card.score, card.found_kw = compute_score(
                        card.title, desc, cfg.keywords
                    )
                    if card.score < cfg.ponderado_min:
                        result.discarded += 1
                        continue

            # Match — invocar callback y persistir en historial
            if match_callback:
                msg_id = match_callback(uid, card)
                if msg_id:
                    append_to_history(uid, card.job_id)
                    history.add(card.job_id)
                    result.matched += 1
                else:
                    seen_now.discard(card.job_id)

            processed.append(card)

        except Exception as e:
            log.error(f"[{uid}] Error procesando tarjeta: {e}")
            continue

    return processed, hits_cargo
