"""
state.py
--------
Gestión del estado global en memoria del sistema.

Responsabilidades:
    - Almacenamiento y acceso a configuración activa por usuario (UserSearchConfig).
    - Estado del wizard paso a paso por usuario (WizardState).
    - Métricas de sesión por usuario (UserStats).
    - Eventos de threading para control del modo automático.
    - Message IDs del botón de stop para limpieza de UI.
    - Flag global de modo diagnóstico (solo admin).

Decisión de arquitectura:
    El estado se mantiene en dicts indexados por UID en lugar de una base
    de datos o cache externo para mantener dependencias mínimas y latencia
    cero en lecturas. La pérdida de estado en reinicios del proceso es
    aceptable dado que el estado de búsqueda es efímero por diseño:
    el historial de vacantes (persistencia real) vive en storage.py.

    Todas las funciones de acceso garantizan inicialización lazy:
    el estado se crea con valores por defecto en el primer acceso,
    eliminando la necesidad de registro explícito de usuarios.
"""

from __future__ import annotations

import threading
from datetime import datetime

from src.config import UserSearchConfig, UserStats, WizardState

# ---------------------------------------------------------------------------
# Stores globales — indexados por UID (int)
# ---------------------------------------------------------------------------

_configs:   dict[int, UserSearchConfig] = {}
_wizards:   dict[int, WizardState]      = {}
_stats:     dict[int, UserStats]        = {}
_auto_events: dict[int, threading.Event] = {}
_stop_msg_ids: dict[int, int | None]    = {}

# Flag global: cuando está activo, solo el admin puede interactuar con el bot.
diagnostic_mode: threading.Event = threading.Event()


# ---------------------------------------------------------------------------
# Acceso con inicialización lazy
# ---------------------------------------------------------------------------

def get_config(uid: int) -> UserSearchConfig:
    """
    Retorna la configuración de búsqueda activa del usuario.
    Inicializa con valores por defecto si es el primer acceso.
    """
    if uid not in _configs:
        _configs[uid] = UserSearchConfig()
    return _configs[uid]


def get_wizard(uid: int) -> WizardState:
    """
    Retorna el estado del wizard activo del usuario.
    Inicializa con estado limpio si es el primer acceso.
    """
    if uid not in _wizards:
        _wizards[uid] = WizardState()
    return _wizards[uid]


def get_stats(uid: int) -> UserStats:
    """
    Retorna las métricas de sesión del usuario.
    Inicializa contadores en cero en el primer acceso.
    """
    if uid not in _stats:
        _stats[uid] = UserStats()
    return _stats[uid]


def get_auto_event(uid: int) -> threading.Event:
    """
    Retorna el Event de threading que controla el modo automático del usuario.

    El evento activo (set) indica que el loop automático debe continuar.
    Llamar a .clear() desde cualquier contexto detiene el loop de forma segura.
    """
    if uid not in _auto_events:
        _auto_events[uid] = threading.Event()
    return _auto_events[uid]


def get_stop_msg_id(uid: int) -> int | None:
    """Retorna el message_id del botón de stop activo, o None si no existe."""
    return _stop_msg_ids.get(uid)


def set_stop_msg_id(uid: int, msg_id: int | None) -> None:
    """Registra el message_id del botón de stop para limpieza posterior de UI."""
    _stop_msg_ids[uid] = msg_id


def pop_stop_msg_id(uid: int) -> int | None:
    """Retorna y elimina el message_id del botón de stop registrado."""
    return _stop_msg_ids.pop(uid, None)


# ---------------------------------------------------------------------------
# Reset de estado por usuario
# ---------------------------------------------------------------------------

def reset_user(uid: int) -> None:
    """
    Reinicia completamente el estado en memoria de un usuario.

    Resetea configuración, wizard y detiene el modo automático si estaba activo.
    No afecta stats de sesión ni datos persistidos en disco (storage.py).

    Llamado en:
        - /inicio (hard reset explícito del usuario).
        - conf_no (el usuario cancela la configuración).
        - stop_auto (el usuario detiene el modo automático).
    """
    _configs[uid] = UserSearchConfig()
    _wizards[uid] = WizardState()

    if uid in _auto_events:
        _auto_events[uid].clear()


# ---------------------------------------------------------------------------
# Actualización de stats
# ---------------------------------------------------------------------------

def record_sweep_result(uid: int, matched: int) -> bool:
    """
    Actualiza las métricas de sesión tras completar un barrido.

    Args:
        uid:     ID del usuario.
        matched: Número de matches encontrados en el barrido.

    Returns:
        True si se alcanzaron 3 barridos consecutivos sin matches
        (señal para notificar al usuario que ajuste sus keywords).
        El contador se reinicia automáticamente al retornar True.
    """
    stats = get_stats(uid)
    stats.barridos_totales += 1
    stats.matches_totales  += matched

    if matched == 0:
        stats.barridos_sin_match += 1
    else:
        stats.barridos_sin_match = 0

    if stats.barridos_sin_match >= 3:
        stats.barridos_sin_match = 0
        return True

    return False
