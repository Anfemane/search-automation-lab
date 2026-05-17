"""
storage.py
----------
Capa de persistencia en disco del sistema.

Responsabilidades:
    - Control de acceso: usuarios aprobados y bloqueados (.txt planos).
    - Historial de vacantes vistas por usuario (deduplicación O(1)).
    - Record personal: últimas N vacantes guardadas por el usuario.
    - Cooldown post hard stop: timestamp de expiración en disco.

Decisión de arquitectura:
    Se usa almacenamiento en archivos planos (.txt) en lugar de una base
    de datos relacional para mantener dependencias mínimas y despliegue
    sin infraestructura externa. SQLite fue evaluado y descartado en esta
    fase por overhead innecesario dado el volumen de usuarios objetivo.

Todos los paths de archivo se construyen a partir del UID del usuario,
garantizando aislamiento total entre sesiones.
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime

from src.config import (
    ADMIN_ID,
    COOLDOWN_MAX_SECONDS,
    COOLDOWN_MIN_SECONDS,
    MAX_RECORD_SIZE,
)


# ---------------------------------------------------------------------------
# Helpers de path — un único lugar para cambiar la convención de nombres
# ---------------------------------------------------------------------------

def _path_history(uid: int) -> str:
    return f"historial_{uid}.txt"

def _path_record(uid: int) -> str:
    return f"record_{uid}.txt"

def _path_cooldown(uid: int) -> str:
    return f"cooldown_{uid}.txt"

_PATH_USERS:    str = "usuarios.txt"
_PATH_BLOCKED:  str = "bloqueados.txt"


# ---------------------------------------------------------------------------
# Utilidades de lectura/escritura de sets de UIDs
# ---------------------------------------------------------------------------

def _load_uid_set(filepath: str) -> set[int]:
    """Lee un archivo de UIDs (uno por línea) y lo retorna como set[int]."""
    if not os.path.exists(filepath):
        return set()
    with open(filepath, "r", encoding="utf-8") as f:
        return {int(line.strip()) for line in f if line.strip().isdigit()}


def _save_uid_set(filepath: str, data: set[int]) -> None:
    """Persiste un set de UIDs en disco, uno por línea."""
    with open(filepath, "w", encoding="utf-8") as f:
        for uid in data:
            f.write(f"{uid}\n")


# ---------------------------------------------------------------------------
# Control de acceso
# ---------------------------------------------------------------------------

def load_approved() -> set[int]:
    """
    Retorna el set de UIDs aprobados, incluyendo siempre al ADMIN_ID.
    El admin no se persiste en el archivo para evitar inconsistencias.
    """
    return _load_uid_set(_PATH_USERS) | {ADMIN_ID}


def load_blocked() -> set[int]:
    """Retorna el set de UIDs bloqueados explícitamente."""
    return _load_uid_set(_PATH_BLOCKED)


def approve_user(uid: int) -> None:
    """Agrega un UID al set de aprobados y persiste. No afecta al admin."""
    approved = load_approved()
    approved.add(uid)
    approved.discard(ADMIN_ID)   # el admin no se escribe en el archivo
    _save_uid_set(_PATH_USERS, approved)


def block_user(uid: int) -> None:
    """Agrega un UID al set de bloqueados y persiste."""
    blocked = load_blocked()
    blocked.add(uid)
    _save_uid_set(_PATH_BLOCKED, blocked)


def unblock_user(uid: int) -> None:
    """Elimina un UID del set de bloqueados y persiste."""
    blocked = load_blocked()
    blocked.discard(uid)
    _save_uid_set(_PATH_BLOCKED, blocked)


def has_access(uid: int) -> bool:
    """
    Evalúa si un UID tiene acceso activo al sistema.

    Precedencia:
        1. ADMIN_ID → siempre True.
        2. Bloqueado explícitamente → False.
        3. Presente en aprobados → True.
        4. Default → False.
    """
    if uid == ADMIN_ID:
        return True
    if uid in load_blocked():
        return False
    return uid in load_approved()


# ---------------------------------------------------------------------------
# Historial de vacantes (deduplicación)
# ---------------------------------------------------------------------------

def load_history(uid: int) -> set[str]:
    """
    Carga el historial de job_ids ya vistos por el usuario.

    El historial es el mecanismo central de deduplicación. En barridos
    automáticos recurrentes, permite descartar vacantes ya procesadas
    en O(1) sin requests HTTP adicionales, reduciendo agresividad de scraping.
    """
    path = _path_history(uid)
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def append_to_history(uid: int, job_id: str) -> None:
    """Agrega un job_id al historial en modo append. No carga el set completo."""
    with open(_path_history(uid), "a", encoding="utf-8") as f:
        f.write(f"{job_id}\n")


# ---------------------------------------------------------------------------
# Record personal (vacantes guardadas)
# ---------------------------------------------------------------------------

def save_to_record(uid: int, job_id: str, title: str, link: str) -> str:
    """
    Persiste una vacante en el record personal del usuario.

    Mantiene un máximo de MAX_RECORD_SIZE entradas (FIFO).
    Retorna un mensaje de confirmación con la posición actual.

    Args:
        uid:    ID del usuario.
        job_id: Identificador normalizado de la vacante.
        title:  Título del puesto.
        link:   URL limpia de la vacante.

    Returns:
        String de confirmación para mostrar al usuario.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    path      = _path_record(uid)
    lines: list[str] = []

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]

    lines.append(f"{job_id}|{title}|{link}|{timestamp}")

    # Ventana deslizante: descarta las entradas más antiguas si supera el límite.
    if len(lines) > MAX_RECORD_SIZE:
        lines = lines[-MAX_RECORD_SIZE:]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return f"Oferta *{len(lines)} de {MAX_RECORD_SIZE}* guardada."


def load_record(uid: int) -> list[dict[str, str]]:
    """
    Carga el record personal del usuario como lista de dicts.

    Returns:
        Lista de dicts con claves: id, titulo, link, fecha.
        Lista vacía si el archivo no existe o está corrupto.
    """
    path = _path_record(uid)
    if not os.path.exists(path):
        return []

    result: list[dict[str, str]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = line.strip().split("|")
            if len(parts) == 4:
                result.append({
                    "id":     parts[0],
                    "titulo": parts[1],
                    "link":   parts[2],
                    "fecha":  parts[3],
                })
    return result


def delete_user_data(uid: int) -> None:
    """Elimina historial y record del usuario. Operación irreversible."""
    for path in [_path_history(uid), _path_record(uid)]:
        if os.path.exists(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# Cooldown post hard stop
# ---------------------------------------------------------------------------

def activate_cooldown(uid: int) -> int:
    """
    Escribe en disco el timestamp de expiración del cooldown.

    La duración se randomiza dentro del rango configurado para evitar
    que múltiples usuarios bloqueados simultáneamente relancen barridos
    pesados al mismo tiempo (efecto manada sobre el servidor de origen).

    Returns:
        Segundos de duración del cooldown activado.
    """
    duration   = random.randint(COOLDOWN_MIN_SECONDS, COOLDOWN_MAX_SECONDS)
    expires_at = time.time() + duration

    with open(_path_cooldown(uid), "w", encoding="utf-8") as f:
        f.write(str(expires_at))

    return duration


def cooldown_remaining_seconds(uid: int) -> int:
    """
    Retorna los segundos restantes de cooldown activo.

    Returns:
        Segundos restantes. 0 si no hay cooldown o ya expiró.
        El archivo de cooldown se elimina automáticamente al expirar.
    """
    path = _path_cooldown(uid)
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            expires_at = float(f.read().strip())
        remaining = int(expires_at - time.time())
        if remaining <= 0:
            os.remove(path)
            return 0
        return remaining
    except Exception:
        return 0


def is_in_cooldown(uid: int) -> bool:
    """Retorna True si el usuario tiene un cooldown activo en este momento."""
    return cooldown_remaining_seconds(uid) > 0


# ---------------------------------------------------------------------------
# Utilidades de presentación
# ---------------------------------------------------------------------------

def relative_time(date_str: str) -> str:
    """
    Convierte un timestamp almacenado en formato legible relativo.

    Args:
        date_str: Fecha en formato '%Y-%m-%d %H:%M'.

    Returns:
        String relativo: 'hace Xh', 'ayer', 'hace X días'.
        Retorna el string original si el formato no es parseable.
    """
    try:
        date  = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        delta = datetime.now() - date
        days  = delta.days
        hours = delta.seconds // 3600

        if days == 0:
            return f"hace {hours}h" if hours > 0 else "hace menos de 1h"
        if days == 1:
            return "ayer"
        return f"hace {days} días"
    except Exception:
        return date_str
