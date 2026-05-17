"""
config.py
---------
Centraliza toda la configuración del sistema: variables de entorno,
constantes operacionales y modelos de datos (Pydantic V2).

Orden de carga:
    1. Variables de entorno via python-dotenv (.env en raíz del proyecto).
    2. Constantes derivadas calculadas en tiempo de importación.
    3. Modelos Pydantic que validan y tipan el estado por usuario.

Ningún otro módulo debe leer os.getenv() directamente.
"""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

load_dotenv()


# ---------------------------------------------------------------------------
# Variables de entorno — credenciales y endpoints externos
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
ADMIN_ID: int       = int(os.getenv("ADMIN_ID", "0"))
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# Modelo y endpoint de la API de Gemini.
# Actualizar GEMINI_MODEL en .env para cambiar versión sin tocar código.
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_URL: str   = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


# ---------------------------------------------------------------------------
# Constantes de scoring y categorías de keywords
# ---------------------------------------------------------------------------

# Pesos brutos por categoría declarada por el usuario.
# La normalización a escala 0-100 se calcula dinámicamente en matching.py
# según la combinación real de keywords configuradas.
CATEGORY_WEIGHTS: dict[str, int] = {
    "esencial":   40,
    "importante": 35,
    "bonus":      25,
}

CATEGORY_ORDER: list[str] = ["esencial", "importante", "bonus"]

# Límites del wizard de keywords.
KEYWORDS_MAX: int = 4
KEYWORDS_MIN: int = 2

# Mapeo de nivel de exigencia declarado → umbral interno de ponderado mínimo (0-100).
STRICTNESS_THRESHOLDS: dict[str, int] = {
    "flexible":   40,
    "balanceado": 60,
    "estricto":   80,
}

# Límite de caracteres para input de perfil hacia la IA.
AI_MAX_CHARS: int = 2000


# ---------------------------------------------------------------------------
# Constantes de scraping y control de flujo
# ---------------------------------------------------------------------------

# Pool de User-Agents para rotación en cada request HTTP.
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# Rango de paginación para barrido completo (offset LinkedIn, paso 25).
FULL_SWEEP_PAGES: range = range(0, 550, 25)

# Hard stop: máximo de vacantes analizadas por barrido antes de activar cooldown.
HARD_STOP_LIMIT: int = 500

# Cooldown post hard stop: rango en segundos (randomizado por usuario).
COOLDOWN_MIN_SECONDS: int = 2700   # 45 min
COOLDOWN_MAX_SECONDS: int = 3600   # 60 min

# Reintentos y tolerancia a errores HTTP 429.
MAX_RETRIES:     int = 2
MAX_429_STREAK:  int = 3

# Ventanas temporales disponibles para el usuario (clave → horas reales).
TIME_RANGE_HOURS: dict[str, int] = {
    "1h":  1,
    "5h":  5,
    "10h": 10,
    "24h": 24,
}

# Parámetros URL de LinkedIn por ventana temporal.
# Todos los modos con filtro de tiempo usan f_TPR=r86400 (24h) como base;
# el filtro fino se aplica en matching.py sobre el innerText del <time>.
TIME_RANGE_URL_PARAMS: dict[str, str] = {
    "1h":         "f_TPR=r86400",
    "5h":         "f_TPR=r86400",
    "10h":        "f_TPR=r86400",
    "24h":        "f_TPR=r86400",
    "relevancia": "sortBy=R",
}

# Máximo de vacantes guardadas en el record personal del usuario.
MAX_RECORD_SIZE: int = 10

# URL pública de documentación técnica del proyecto.
README_URL: str = (
    "https://github.com/Anfemane/linkedin-job-scraper-bot/blob/main/PAGINAS_README.md"
)


# ---------------------------------------------------------------------------
# Modelos de estado por usuario (Pydantic V2)
# ---------------------------------------------------------------------------

SearchTime = Literal["1h", "5h", "10h", "24h", "relevancia"]
StrictnessLevel = Literal["flexible", "balanceado", "estricto"]
KeywordCategory = Literal["esencial", "importante", "bonus"]
WizardMode = Literal["manual", "auto"]
WizardEntry = Literal["ia", "manual"]
WizardStep = Literal[
    "pedir_cargo",
    "pedir_keyword",
    "pedir_perfil_ia",
    "pedir_perfil_wizard",
]


class UserSearchConfig(BaseModel):
    """
    Configuración de búsqueda activa del usuario.

    Generada durante el wizard y consumida por el scraper en cada barrido.
    Se resetea completamente al iniciar una nueva búsqueda (/inicio o conf_no).
    """

    cargo: list[str] = Field(
        default_factory=list,
        description="1-2 términos de cargo usados como keywords en la URL de LinkedIn.",
    )
    keywords: dict[str, KeywordCategory] = Field(
        default_factory=dict,
        description="Mapa {palabra: categoría} configurado por el usuario o sugerido por IA.",
    )
    ponderado_min: int = Field(
        default=60,
        ge=0,
        le=100,
        description="Umbral mínimo de ponderado (0-100) derivado del nivel de exigencia.",
    )
    tiempo: SearchTime = Field(
        default="1h",
        description="Ventana temporal seleccionada por el usuario.",
    )
    horas_auto: int = Field(
        default=1,
        ge=1,
        le=24,
        description="Duración total del modo automático en horas.",
    )
    es_relevancia: bool = Field(
        default=False,
        description="True cuando el modo de tiempo es 'relevancia' (sin filtro temporal).",
    )

    @field_validator("keywords")
    @classmethod
    def validate_keyword_count(cls, v: dict) -> dict:
        if len(v) > KEYWORDS_MAX:
            raise ValueError(f"Máximo {KEYWORDS_MAX} keywords permitidas.")
        return v


class WizardState(BaseModel):
    """
    Estado interno del wizard de configuración paso a paso.

    Persiste en memoria durante la sesión activa del usuario.
    Se limpia al finalizar la configuración o al resetear.
    """

    paso: WizardStep | None = Field(
        default=None,
        description="Paso actual del wizard. None indica que no hay wizard activo.",
    )
    modo: WizardMode | None = Field(
        default=None,
        description="Modo de ejecución elegido: búsqueda única o automática.",
    )
    kw_buffer: dict[str, KeywordCategory] = Field(
        default_factory=dict,
        description="Acumulador de keywords durante el flujo uno-a-uno.",
    )
    entrada_wizard: WizardEntry | None = Field(
        default=None,
        description="Canal de entrada al wizard: IA o manual.",
    )


class UserStats(BaseModel):
    """Métricas acumuladas de uso por usuario durante la sesión activa."""

    barridos_totales:   int = 0
    matches_totales:    int = 0
    barridos_sin_match: int = 0
