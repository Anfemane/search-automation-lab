"""
bot.py
------
Capa de interfaz de usuario sobre Telegram.

Responsabilidades:
    - Cliente HTTP de la Bot API de Telegram (sin librerías externas).
    - Listener de updates con long polling.
    - Despacho de comandos (/inicio, /guia, /stop, comandos admin).
    - Manejo de callbacks de botones inline (wizard, vacantes, admin).
    - Manejo de texto libre (pasos del wizard).
    - Orquestación del flujo de búsqueda: wizard → confirmación → barrido.
    - Modo automático con threading y control por Event.

Decisión de arquitectura:
    El bot no usa librerías de alto nivel (python-telegram-bot, aiogram)
    para mantener dependencias mínimas y control total sobre el flujo HTTP,
    lo cual fue determinante en el entorno de infraestructura restringida
    donde el proyecto fue desarrollado originalmente.

    Toda la lógica de negocio (scoring, scraping, persistencia) está
    delegada a los módulos correspondientes. bot.py solo coordina flujo
    y mensajería.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from src.ai_client import WizardConfig, call_advisor, call_wizard
from src.config import (
    ADMIN_ID,
    AI_MAX_CHARS,
    KEYWORDS_MAX,
    KEYWORDS_MIN,
    MAX_RECORD_SIZE,
    README_URL,
    STRICTNESS_THRESHOLDS,
    TELEGRAM_TOKEN,
    KeywordCategory,
)
from src.matching import score_label
from src.scraper import JobCard, SweepResult, run_sweep
from src.state import (
    diagnostic_mode,
    get_auto_event,
    get_config,
    get_stop_msg_id,
    get_wizard,
    pop_stop_msg_id,
    record_sweep_result,
    reset_user,
    set_stop_msg_id,
)
from src.storage import (
    approve_user,
    block_user,
    cooldown_remaining_seconds,
    delete_user_data,
    has_access,
    load_record,
    relative_time,
    save_to_record,
    unblock_user,
    load_approved,
    load_blocked,
    activate_cooldown,
)

log = logging.getLogger(__name__)

_BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


# ---------------------------------------------------------------------------
# Telegram API — cliente HTTP mínimo
# ---------------------------------------------------------------------------

def send(uid: int, text: str, markup: dict = None) -> int | None:
    """
    Envía un mensaje de texto al usuario vía Telegram Bot API.

    Args:
        uid:    Chat ID del destinatario.
        text:   Texto en formato Markdown.
        markup: reply_markup opcional (inline keyboard).

    Returns:
        message_id del mensaje enviado. None si falla.
    """
    payload = {"chat_id": uid, "text": text, "parse_mode": "Markdown"}
    if markup:
        payload["reply_markup"] = markup
    try:
        resp = requests.post(f"{_BASE_URL}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("message_id")
    except Exception as e:
        log.error(f"send [{uid}]: {e}")
        return None


def delete_msg(uid: int, msg_id: int) -> None:
    """Elimina un mensaje del chat. Fallo silencioso si el mensaje ya no existe."""
    if not msg_id:
        return
    try:
        requests.post(
            f"{_BASE_URL}/deleteMessage",
            json={"chat_id": uid, "message_id": msg_id},
            timeout=5,
        )
    except Exception:
        pass


def clear_markup(uid: int, msg_id: int) -> None:
    """Elimina los botones inline de un mensaje existente sin borrar el texto."""
    if not msg_id:
        return
    try:
        requests.post(
            f"{_BASE_URL}/editMessageReplyMarkup",
            json={"chat_id": uid, "message_id": msg_id, "reply_markup": {"inline_keyboard": []}},
            timeout=5,
        )
    except Exception:
        pass


def answer_callback(callback_id: str) -> None:
    """Responde un callback query para eliminar el spinner de carga en el cliente."""
    try:
        requests.post(
            f"{_BASE_URL}/answerCallbackQuery",
            json={"callback_query_id": callback_id},
            timeout=5,
        )
    except Exception:
        pass


def get_updates(offset: int) -> list:
    """Obtiene updates pendientes via long polling. Retorna lista vacía si falla."""
    try:
        resp = requests.get(
            f"{_BASE_URL}/getUpdates",
            params={"offset": offset, "timeout": 10},
            timeout=15,
        )
        return resp.json().get("result", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Menús y mensajes de UI
# ---------------------------------------------------------------------------

def _menu_main(uid: int) -> None:
    """Menú principal. Limpia el botón de stop si estaba activo."""
    old = pop_stop_msg_id(uid)
    if old:
        clear_markup(uid, old)
    send(uid, "🏠 *Menú Principal*", markup={"inline_keyboard": [
        [
            {"text": "🔍 Nueva búsqueda", "callback_data": "mp_busqueda"},
            {"text": "📋 Guardados",       "callback_data": "mp_guardados"},
        ],
        [{"text": "🤖 Sugerencias IA (Beta)", "callback_data": "mp_ia"}],
    ]})


def _menu_mode(uid: int) -> None:
    """Selección de modo de entrada al wizard: IA o manual."""
    send(uid,
        "🚀 *Nueva búsqueda*\n\n"
        "¿Cómo quieres configurar tu búsqueda?\n\n"
        "🤖 *Con IA* — Cuéntale al bot qué buscas en lenguaje natural\n"
        "⚙️ *Manual* — Configura tú mismo cargo, exigencia y palabras clave",
        markup={"inline_keyboard": [[
            {"text": "🤖 Con IA",   "callback_data": "entrada_ia"},
            {"text": "⚙️ Manual",   "callback_data": "entrada_manual"},
        ]]},
    )


def _menu_strictness(uid: int) -> None:
    """Selección de nivel de exigencia del filtro."""
    send(uid,
        "🎯 *¿Qué tan exigente quieres ser en el filtro?*\n\n"
        "• *Flexible* — basta con 1 o 2 palabras clave\n"
        "• *Balanceado* — necesita la mayoría ✅ recomendado\n"
        "• *Estricto* — casi todas deben aparecer",
        markup={"inline_keyboard": [[
            {"text": "😌 Flexible",      "callback_data": "exig_flexible"},
            {"text": "⚖️ Balanceado ✅", "callback_data": "exig_balanceado"},
            {"text": "🎯 Estricto",      "callback_data": "exig_estricto"},
        ]]},
    )


def _menu_time(uid: int) -> None:
    """Selección de ventana temporal de búsqueda."""
    send(uid,
        "⏱️ *¿Qué período quieres buscar?*",
        markup={"inline_keyboard": [
            [
                {"text": "⚡ Última hora",   "callback_data": "tiempo_1h"},
                {"text": "🕔 Últimas 5h",    "callback_data": "tiempo_5h"},
            ],
            [
                {"text": "🕙 Últimas 10h",   "callback_data": "tiempo_10h"},
                {"text": "📅 Último día",    "callback_data": "tiempo_24h"},
            ],
            [{"text": "🔥 Relevancia",       "callback_data": "tiempo_relevancia"}],
        ]},
    )


def _menu_auto_duration(uid: int) -> None:
    """Selección de duración del modo automático."""
    send(uid,
        "🕐 *¿Por cuánto tiempo correr el modo automático?*",
        markup={"inline_keyboard": [[
            {"text": "1 hora",  "callback_data": "dur_1"},
            {"text": "3 horas", "callback_data": "dur_3"},
            {"text": "7 horas", "callback_data": "dur_7"},
        ]]},
    )


def _menu_keyword_prompt(uid: int) -> None:
    """Solicita la siguiente keyword en el flujo uno-a-uno."""
    wiz     = get_wizard(uid)
    buf     = wiz.kw_buffer
    n       = len(buf) + 1
    intro   = (
        "🔑 *Palabras clave — paso a paso*\n\n"
        "Escribe palabras que describan lo que buscas en una vacante.\n"
        "💡 *Sin tildes:* ❌ _Técnico_ → ✅ _Tecnico_\n\n"
        if n == 1 else ""
    )
    suffix = (
        f"\n\n_Puedes terminar con /listo si ya tienes suficientes ({len(buf)} guardadas)_"
        if len(buf) >= KEYWORDS_MIN else ""
    )
    send(uid, f"{intro}✏️ *Keyword {n} de {KEYWORDS_MAX}*\n\nEscribe la palabra clave:{suffix}")


def _menu_keyword_category(uid: int, keyword: str) -> None:
    """Muestra botones de categoría para la keyword recién escrita."""
    wiz    = get_wizard(uid)
    used   = list(wiz.kw_buffer.values())
    buttons = []
    if used.count("esencial") < 1:
        buttons.append({"text": "⭐ Esencial",   "callback_data": f"cat_esencial_{keyword}"})
    if used.count("importante") < 1:
        buttons.append({"text": "✅ Importante", "callback_data": f"cat_importante_{keyword}"})
    buttons.append(    {"text": "➕ Bonus",       "callback_data": f"cat_bonus_{keyword}"})

    send(uid,
        f"*{keyword}* — ¿qué tan importante es esta palabra?\n\n"
        "⭐ *Esencial* — si no está en el título, la vacante se descarta\n"
        "✅ *Importante* — suma bastante al puntaje final\n"
        "➕ *Bonus* — suma al puntaje, no es determinante\n\n"
        "_Solo puedes tener 1 Esencial y 1 Importante._",
        markup={"inline_keyboard": [buttons]},
    )


def _show_confirmation(uid: int) -> None:
    """Muestra el resumen completo de configuración antes de arrancar."""
    cfg = get_config(uid)
    wiz = get_wizard(uid)

    time_labels = {
        "1h": "Última hora", "5h": "Últimas 5h",
        "10h": "Últimas 10h", "24h": "Último día", "relevancia": "Relevancia",
    }
    exig_labels = {40: "Flexible 😌", 60: "Balanceado ⚖️", 80: "Estricto 🎯"}
    cat_icons   = {"esencial": "⭐", "importante": "✅", "bonus": "➕"}

    kw = cfg.keywords
    if kw:
        order    = {"esencial": 0, "importante": 1, "bonus": 2}
        sorted_kw = sorted(kw.items(), key=lambda x: order.get(x[1], 3))
        from src.matching import _normalize_weights
        weights   = _normalize_weights(kw)
        kw_lines  = "\n".join(
            f"   {cat_icons.get(cat, '•')} {word} — {weights[word]}%"
            for word, cat in sorted_kw
        )
    else:
        kw_lines = "   —"

    cargo_txt = ", ".join(cfg.cargo) if cfg.cargo else "—"
    summary   = (
        "📋 *Resumen de tu búsqueda*\n\n"
        f"💼 *Cargo:* `{cargo_txt}`\n"
        f"⏱️ *Periodo:* {time_labels.get(cfg.tiempo, cfg.tiempo)}\n"
        f"🎯 *Exigencia:* {exig_labels.get(cfg.ponderado_min, f'{cfg.ponderado_min}%')}\n\n"
        f"🔑 *Palabras clave:*\n{kw_lines}\n"
    )
    if wiz.modo == "auto":
        summary += f"\n🕐 *Duración automático:* {cfg.horas_auto}h\n"
    summary += "\n¿Arrancamos?"

    send(uid, summary, markup={"inline_keyboard": [[
        {"text": "🚀 Confirmar",       "callback_data": "conf_si"},
        {"text": "↩️ Volver al menú",  "callback_data": "conf_no"},
    ]]})


def _show_saved(uid: int) -> None:
    """Muestra las vacantes guardadas en el record personal del usuario."""
    jobs = load_record(uid)
    if not jobs:
        send(uid, "📭 Sin guardados aún. Las vacantes que guardes aparecerán aquí.")
    else:
        msg = "📋 *Vacantes guardadas*\n\n"
        for i, job in enumerate(reversed(jobs), 1):
            msg += (
                f"*{i}.* {job['titulo']}\n"
                f"🕐 {relative_time(job['fecha'])}\n"
                f"🔗 {job['link']}\n\n"
            )
        send(uid, msg)
    _menu_main(uid)


def _send_stop_button(uid: int) -> None:
    """Envía el botón de stop del modo automático, reemplazando el anterior si existe."""
    old = get_stop_msg_id(uid)
    if old:
        clear_markup(uid, old)
    msg_id = send(uid,
        "⏹️ Barrido automático en curso. Presiona para detener:",
        markup={"inline_keyboard": [[
            {"text": "⏹️ Detener barrido", "callback_data": "stop_auto"}
        ]]},
    )
    set_stop_msg_id(uid, msg_id)


def _greet_new_user(uid: int, name: str) -> None:
    """Bienvenida al usuario recién aprobado."""
    send(uid,
        f"✅ *¡Acceso aprobado, {name}!*\n\n"
        "Bienvenido al *Job Scraper Bot* 🤖\n\n"
        "Escaneo vacantes en LinkedIn Colombia y te notifico cuando encuentro "
        "algo que coincida con tus criterios.\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📋 *Comandos disponibles:*\n\n"
        "/inicio   — vuelve al menú principal\n"
        "/registrar — solicitar acceso\n"
        "/guia     — ver la guía de uso",
    )
    time.sleep(1)
    _menu_main(uid)


def _send_guide(uid: int) -> None:
    """Envía la guía de uso del bot."""
    send(uid,
        "📖 *Guía de Uso — Job Scraper Bot*\n\n"
        "El bot filtra vacantes en tres pasos:\n\n"
        "*1️⃣ Cargo:* 1-2 palabras → LinkedIn busca con lógica OR\n"
        "*2️⃣ Portero:* la keyword de menor peso debe estar en el título\n"
        "*3️⃣ Ponderado:* suma de pesos de keywords en título + descripción\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📄 *Límites de escaneo:*\n\n"
        "• Hasta 500 vacantes por barrido\n"
        "• El bot para antes si no hay resultados nuevos\n\n"
        f"[📘 Documentación técnica]({README_URL})\n\n"
        "━━━━━━━━━━━━━━━\n"
        "💡 *Tip:* No uses tildes. ❌ _Bilingüe_ → ✅ _Bilingue_",
    )
    _menu_main(uid)


# ---------------------------------------------------------------------------
# Callbacks de vacantes — botones inline en cada match
# ---------------------------------------------------------------------------

def _build_match_message(card: JobCard, no_filter: bool) -> str:
    """Construye el mensaje de match para enviar al usuario."""
    if no_filter:
        label    = "🧪 *Test — sin filtros*"
        kw_line  = "—"
    else:
        label    = score_label(card.score)
        kw_line  = ", ".join(
            f"{k} ({v}%)"
            for k, v in sorted(card.found_kw.items(), key=lambda x: -x[1])
        ) if card.found_kw else "—"

    return (
        f"{label}\n\n"
        f"🏢 *{card.company}*\n"
        f"📌 {card.title}\n"
        f"🕐 {card.date_display}\n"
        f"🔑 `{kw_line}`\n"
        f"🔗 {card.link}"
    )


def _match_callback(uid: int, card: JobCard, no_filter: bool = False) -> int | None:
    """
    Callback invocado por el scraper por cada vacante que supera los filtros.
    Envía el mensaje de match con botones de acción al usuario.

    Returns:
        message_id del mensaje enviado. None si falla.
    """
    return send(
        uid,
        _build_match_message(card, no_filter),
        markup={"inline_keyboard": [[
            {"text": "✅ Me interesa",    "callback_data": f"interesa_{card.job_id}"},
            {"text": "❌ No me interesa", "callback_data": f"no_interesa_{card.job_id}"},
            {"text": "🔖 Guardar",        "callback_data": f"guardar_{card.job_id}"},
        ]]},
    )


# ---------------------------------------------------------------------------
# Orquestación de barridos
# ---------------------------------------------------------------------------

def _run_manual_sweep(uid: int) -> None:
    """Ejecuta un barrido único en thread separado."""
    cfg    = get_config(uid)
    result = run_sweep(
        uid=uid,
        cfg=cfg,
        progress_callback=lambda idx, total: send(uid, f"🔍 Página *{idx} de {total}*..."),
        match_callback=lambda u, card: _match_callback(u, card),
    )
    _handle_sweep_result(uid, result, mode="manual")


def _run_auto_loop(uid: int, hours: int) -> None:
    """
    Loop del modo automático. Ejecuta barridos periódicos durante `hours` horas.

    La deduplicación por historial garantiza que barridos recurrentes
    descarten en O(1) lo ya visto, sin requests HTTP adicionales.
    """
    auto_ev       = get_auto_event(uid)
    total_seconds = hours * 3600
    start_time    = time.time()

    while True:
        if not auto_ev.is_set():
            return
        if time.time() - start_time >= total_seconds:
            break

        cfg    = get_config(uid)
        result = run_sweep(
            uid=uid,
            cfg=cfg,
            progress_callback=lambda idx, total: send(uid, f"🔍 Página *{idx} de {total}*..."),
            match_callback=lambda u, card: _match_callback(u, card),
        )
        _handle_sweep_result(uid, result, mode="auto")

        if not auto_ev.is_set():
            return
        if time.time() - start_time >= total_seconds:
            break

        remaining = int(total_seconds - (time.time() - start_time))
        wait      = min(3600, remaining)

        send(uid, "⏳ Próximo barrido en *1 hora*.")
        _send_stop_button(uid)

        for _ in range(wait // 10):
            time.sleep(10)
            if not auto_ev.is_set():
                return

    # Finalización normal del loop automático
    auto_ev.clear()
    old = pop_stop_msg_id(uid)
    if old:
        clear_markup(uid, old)
    send(uid, f"✅ *Modo automático finalizado* — {hours}h completadas.")
    _menu_main(uid)


def _handle_sweep_result(uid: int, result: SweepResult, mode: str) -> None:
    """
    Procesa el resultado de un barrido y notifica al usuario.

    Actualiza stats, notifica hard stop si aplica, y muestra resumen.
    """
    if result.analyzed == 0:
        send(uid, "⚠️ El bot no encontró vacantes. Posible cambio en LinkedIn.")
        return

    should_suggest = record_sweep_result(uid, result.matched)

    send(uid,
        f"🚀 *Barrido completado*\n"
        f"🔍 Revisadas: {result.analyzed}\n"
        f"🗑️ Descartadas: {result.discarded}\n"
        f"✅ Matches: {result.matched}",
    )

    if should_suggest:
        send(uid, "ℹ️ 3 barridos sin matches. Considera ajustar las keywords.")

    if result.hard_stopped:
        minutes = result.cooldown_seconds // 60
        send(uid,
            f"🛑 *El bot ha llegado a su límite seguro de escaneo.*\n\n"
            f"Nueva búsqueda restringida por aproximadamente *{minutes} minutos*.\n\n"
            "No te perderás de nada — el buscador tiene memoria y deduplicación robusta.\n\n"
            "_500 vacantes revisadas en un solo barrido es una locura a nivel humano, chaval_ 😄",
            markup={"inline_keyboard": [[
                {"text": "✅ Entendido",         "callback_data": "hardstop_ok"},
                {"text": "🔍 Quiero saber más",  "callback_data": "hardstop_info"},
            ]]},
        )
        return

    if mode == "manual":
        send(uid,
            "⚠️ *Sobre el uso frecuente:*\n\n"
            "No hagas barridos manuales muy seguidos — LinkedIn puede detectar "
            "el patrón. Para monitoreo continuo usa el *modo automático*.",
            markup={"inline_keyboard": [[
                {"text": "🏠 Volver al menú", "callback_data": "hardstop_ok"}
            ]]},
        )


def _start_search(uid: int) -> None:
    """Arranca la búsqueda según el modo configurado en el wizard."""
    wiz = get_wizard(uid)
    cfg = get_config(uid)
    send(uid, "🚀 *Configuración lista. Arrancando...*")

    if wiz.modo == "auto":
        hours = cfg.horas_auto
        get_auto_event(uid).set()
        send(uid, f"🔄 *Modo automático activo* — {hours}h")
        threading.Thread(
            target=_run_auto_loop, args=(uid, hours), daemon=True
        ).start()
    else:
        threading.Thread(
            target=_run_manual_sweep, args=(uid,), daemon=True
        ).start()


# ---------------------------------------------------------------------------
# Aplicación de config generada por IA (wizard mode)
# ---------------------------------------------------------------------------

def _apply_wizard_ai_config(uid: int, profile_text: str) -> None:
    """
    Worker de thread: llama a call_wizard(), valida y aplica la config al usuario.
    Si falla, redirige al wizard manual.
    """
    result = call_wizard(profile_text)

    if result is None:
        send(uid, "⚠️ La IA no pudo generar una configuración. Te redirigimos al modo manual.")
        wiz = get_wizard(uid)
        wiz.entrada_wizard = "manual"
        wiz.paso           = None
        _menu_strictness(uid)
        return

    cfg = get_config(uid)
    wiz = get_wizard(uid)

    cfg.cargo         = result.cargo
    cfg.keywords      = result.keywords
    cfg.ponderado_min = result.ponderado_min
    wiz.paso          = None

    cat_icons  = {"esencial": "⭐", "importante": "✅", "bonus": "➕"}
    exig_label = {"flexible": "Flexible 😌", "balanceado": "Balanceado ⚖️",
                  "estricto": "Estricto 🎯"}.get(result.exigencia, result.exigencia)
    kw_lines   = "\n".join(
        f"   {cat_icons.get(cat, '•')} {word}"
        for word, cat in result.keywords.items()
    )

    send(uid,
        f"🤖 *La IA configuró tu búsqueda:*\n\n"
        f"💼 *Cargo:* `{', '.join(result.cargo)}`\n"
        f"🎯 *Exigencia:* {exig_label}\n\n"
        f"🔑 *Palabras clave:*\n{kw_lines}\n\n"
        "_¿Te parece bien? Elige el periodo y arrancamos._",
    )
    _menu_time(uid)


# ---------------------------------------------------------------------------
# Manejo de callbacks inline
# ---------------------------------------------------------------------------

def handle_callback(uid: int, data: str, msg_id: int) -> None:
    """
    Despacha callbacks de botones inline al handler correspondiente.

    Args:
        uid:    ID del usuario que presionó el botón.
        data:   callback_data del botón.
        msg_id: message_id del mensaje que contenía el botón.
    """
    cfg = get_config(uid)
    wiz = get_wizard(uid)
    delete_msg(uid, msg_id)

    # Menú principal
    if data == "mp_busqueda":
        remaining = cooldown_remaining_seconds(uid)
        if remaining > 0 and uid != ADMIN_ID:
            send(uid,
                f"⏳ *Nueva Búsqueda no disponible aún.*\n\n"
                f"El bot necesita *{remaining // 60} minutos* más de reposo.",
            )
            return
        reset_user(uid)
        _menu_mode(uid)

    elif data == "mp_guardados":
        _show_saved(uid)

    elif data == "mp_ia":
        send(uid,
            "🤖 *Sugerencias IA — Beta*\n\n"
            "Escribe tu perfil: estudios, experiencia y el puesto que buscas.\n\n"
            "La IA te dará:\n"
            "• Consejos estratégicos de búsqueda\n"
            "• Cargos que se ajustan a ti\n"
            "• 15 palabras clave para usar en el bot\n\n"
            f"⚠️ Máximo *{AI_MAX_CHARS} caracteres* por consulta.\n\n"
            "¿Deseas continuar?",
            markup={"inline_keyboard": [[
                {"text": "✅ Entendido, continuar", "callback_data": "ia_continuar"},
                {"text": "↩️ Volver al menú",       "callback_data": "ia_cancelar"},
            ]]},
        )

    elif data == "ia_continuar":
        wiz.paso = "pedir_perfil_ia"
        send(uid,
            f"✍️ *Escribe tu perfil y el puesto que buscas:*\n\n"
            f"_Máximo {AI_MAX_CHARS} caracteres._",
        )

    elif data == "ia_cancelar":
        _menu_main(uid)

    # Entrada al wizard
    elif data == "entrada_ia":
        wiz.entrada_wizard = "ia"
        send(uid,
            "🤖 *Configuración con IA*\n\n"
            "Cuéntame qué trabajo buscas en tus propias palabras.\n\n"
            "Ejemplo:\n"
            "_Soy tecnico en sistemas, se algo de redes y soporte. "
            "Busco trabajo presencial en Bogota, no quiero ventas_\n\n"
            f"Máximo *{AI_MAX_CHARS} caracteres*.",
        )
        wiz.paso = "pedir_perfil_wizard"

    elif data == "entrada_manual":
        wiz.entrada_wizard = "manual"
        send(uid,
            "⚙️ *Configuración manual*\n\n¿Cómo quieres correr el bot?",
            markup={"inline_keyboard": [[
                {"text": "▶️ Una búsqueda", "callback_data": "modo_manual"},
                {"text": "🔄 Automático",   "callback_data": "modo_auto"},
            ]]},
        )

    # Modo de ejecución
    elif data == "modo_manual":
        wiz.modo = "manual"
        _menu_time(uid)

    elif data == "modo_auto":
        wiz.modo = "auto"
        _menu_time(uid)

    # Ventana temporal
    elif data.startswith("tiempo_"):
        t = data.split("_", 1)[1]
        cfg.tiempo        = t
        cfg.es_relevancia = (t == "relevancia")
        if wiz.modo == "auto":
            _menu_auto_duration(uid)
        else:
            wiz.paso = "pedir_cargo"
            send(uid,
                "💼 *¿Qué cargo o rol buscas?*\n\n"
                "Escribe 1 o 2 términos separados por coma.\n\n"
                "Ejemplo: `Salesforce` — `Data Analyst, Developer`",
            )

    # Duración automático
    elif data.startswith("dur_"):
        cfg.horas_auto = int(data.split("_")[1])
        wiz.paso       = "pedir_cargo"
        send(uid,
            "💼 *¿Qué cargo o rol buscas?*\n\n"
            "Escribe 1 o 2 términos separados por coma.\n\n"
            "Ejemplo: `Salesforce` — `Data Analyst, Developer`",
        )

    # Exigencia
    elif data.startswith("exig_"):
        nivel = data.split("_", 1)[1]
        cfg.ponderado_min = STRICTNESS_THRESHOLDS[nivel]
        wiz.kw_buffer     = {}
        wiz.paso          = "pedir_keyword"
        _menu_keyword_prompt(uid)

    # Categoría de keyword
    elif data.startswith("cat_"):
        _, categoria, keyword = data.split("_", 2)
        wiz.kw_buffer[keyword] = categoria
        cat_name = {"esencial": "⭐ Esencial", "importante": "✅ Importante",
                    "bonus": "➕ Bonus"}.get(categoria, categoria)
        send(uid, f"✅ *{keyword}* guardada como {cat_name}.")

        if len(wiz.kw_buffer) >= KEYWORDS_MAX:
            cfg.keywords = dict(wiz.kw_buffer)
            wiz.paso     = None
            _show_confirmation(uid)
        else:
            wiz.paso = "pedir_keyword"
            _menu_keyword_prompt(uid)

    # Confirmación
    elif data == "conf_si":
        _start_search(uid)

    elif data == "conf_no":
        reset_user(uid)
        _menu_main(uid)

    # Stop automático
    elif data == "stop_auto":
        get_auto_event(uid).clear()
        pop_stop_msg_id(uid)
        send(uid, "⏸️ *Barrido automático detenido.*")
        reset_user(uid)
        _menu_main(uid)

    # Hard stop info
    elif data == "hardstop_ok":
        _menu_main(uid)

    elif data == "hardstop_info":
        send(uid,
            "🛡️ *¿Por qué existe este límite?*\n\n"
            "LinkedIn libra una batalla diaria contra scrapers agresivos. "
            "Los límites del bot están diseñados para llevar tu búsqueda al máximo "
            "sin poner en riesgo el acceso al servicio.\n\n"
            "¿Quieres entender el modelo lógico completo?",
            markup={"inline_keyboard": [[
                {"text": "📘 Documentación técnica", "url": README_URL},
                {"text": "↩️ Volver al menú",        "callback_data": "hardstop_ok"},
            ]]},
        )

    # Admin: aprobar / rechazar
    elif data.startswith("aprobar_"):
        parts    = data.split("_", 2)
        nuevo_uid = int(parts[1])
        nombre   = parts[2] if len(parts) > 2 else str(nuevo_uid)
        approve_user(nuevo_uid)
        _greet_new_user(nuevo_uid, nombre)
        send(ADMIN_ID, f"✅ Usuario `{nuevo_uid}` aprobado.")

    elif data.startswith("rechazar_"):
        nuevo_uid = int(data.split("_")[1])
        send(nuevo_uid, "❌ Tu solicitud de acceso no fue aprobada.")
        send(ADMIN_ID, f"❌ Usuario `{nuevo_uid}` rechazado.")

    # Vacantes — manejadas en el listener directamente
    elif data.startswith(("interesa_", "no_interesa_", "guardar_")):
        pass


# ---------------------------------------------------------------------------
# Manejo de texto libre (pasos del wizard)
# ---------------------------------------------------------------------------

def handle_text(uid: int, text: str) -> None:
    """
    Procesa mensajes de texto libre según el paso activo del wizard.

    Args:
        uid:  ID del usuario.
        text: Texto enviado por el usuario.
    """
    wiz  = get_wizard(uid)
    cfg  = get_config(uid)
    paso = wiz.paso

    # Modo consejero IA
    if paso == "pedir_perfil_ia":
        if len(text) > AI_MAX_CHARS:
            send(uid, f"⚠️ Máximo *{AI_MAX_CHARS} caracteres*. Tienes {len(text)}.")
            return
        wiz.paso = None
        send(uid, "⏳ *Analizando tu perfil...* Esto puede tomar unos segundos.")
        def _advisor_thread():
            resp = call_advisor(text)
            send(uid, f"🤖 *Análisis de tu perfil:*\n\n{resp}")
            _menu_main(uid)
        threading.Thread(target=_advisor_thread, daemon=True).start()

    # Configuración IA para wizard
    elif paso == "pedir_perfil_wizard":
        if len(text) > AI_MAX_CHARS:
            send(uid, f"⚠️ Máximo *{AI_MAX_CHARS} caracteres*. Tienes {len(text)}.")
            return
        wiz.paso = None
        send(uid, "⏳ *Configurando tu búsqueda...* Un momento.")
        threading.Thread(
            target=_apply_wizard_ai_config, args=(uid, text), daemon=True
        ).start()

    # Cargo
    elif paso == "pedir_cargo":
        terms = [t.strip() for t in text.split(",") if t.strip()][:2]
        if not terms:
            send(uid, "⚠️ Escribe al menos un término de búsqueda.")
            return
        cfg.cargo = terms
        send(uid, f"✅ *Cargo:* `{', '.join(terms)}`")
        wiz.paso  = None
        _menu_strictness(uid)

    # Keyword uno a uno
    elif paso == "pedir_keyword":
        keyword = text.strip()
        if len(keyword) < 2:
            send(uid, "⚠️ La palabra clave debe tener al menos 2 caracteres.")
            return
        if len(keyword) > 30:
            send(uid, "⚠️ Máximo 30 caracteres por palabra clave.")
            return
        if "," in keyword:
            send(uid, "⚠️ Escribe *una sola palabra* por mensaje.")
            return
        from src.matching import normalize
        if normalize(keyword) in [normalize(k) for k in wiz.kw_buffer]:
            send(uid, f"⚠️ Ya tienes *{keyword}* en tu lista.")
            return
        _menu_keyword_category(uid, keyword)


# ---------------------------------------------------------------------------
# Manejo de comandos
# ---------------------------------------------------------------------------

def handle_command(uid: int, text: str, name: str) -> None:
    """
    Despacha comandos de texto (/inicio, /guia, /stop, comandos admin).

    Args:
        uid:  ID del usuario.
        text: Texto completo del mensaje (incluye el comando).
        name: Nombre del usuario para mensajes de bienvenida.
    """
    cmd = text.strip().lower().split()[0]

    # Registro — único comando disponible sin acceso
    if cmd == "/registrar":
        if has_access(uid):
            send(uid, "ℹ️ Ya tienes acceso. Usa /inicio para empezar.")
        else:
            send(uid, "📋 Solicitud enviada. Espera la aprobación del administrador.")
            send(ADMIN_ID,
                f"🔔 *Nueva solicitud*\n👤 {name}\n🆔 `{uid}`",
                markup={"inline_keyboard": [[
                    {"text": "✅ Aprobar", "callback_data": f"aprobar_{uid}_{name}"},
                    {"text": "❌ Rechazar", "callback_data": f"rechazar_{uid}"},
                ]]},
            )
        return

    if not has_access(uid):
        send(uid, "🔒 Sin acceso. Escribe /registrar para solicitarlo.")
        return

    if diagnostic_mode.is_set() and uid != ADMIN_ID:
        send(uid, "🔧 *Bot en mantenimiento.* Vuelve en unos minutos.")
        return

    # Comandos de usuario
    if cmd == "/inicio":
        reset_user(uid)
        _menu_main(uid)

    elif cmd == "/guia":
        _send_guide(uid)

    elif cmd == "/listo":
        if wiz := get_wizard(uid):
            if wiz.paso == "pedir_keyword":
                buf = wiz.kw_buffer
                if len(buf) < KEYWORDS_MIN:
                    send(uid, f"⚠️ Necesitas al menos *{KEYWORDS_MIN} palabras clave*. Tienes {len(buf)}.")
                else:
                    get_config(uid).keywords = dict(buf)
                    wiz.paso = None
                    _show_confirmation(uid)
            else:
                send(uid, "ℹ️ No hay una configuración de keywords en curso.")

    elif cmd == "/stop":
        get_auto_event(uid).clear()
        old = pop_stop_msg_id(uid)
        if old:
            clear_markup(uid, old)
        reset_user(uid)
        send(uid, "⏹️ *Barrido detenido.*")
        _menu_main(uid)

    # Comandos exclusivos admin
    elif uid == ADMIN_ID:
        _handle_admin_command(uid, cmd, text)

    else:
        send(uid, "❓ Comando no reconocido. Usa /inicio para volver al menú.")


def _handle_admin_command(uid: int, cmd: str, full_text: str) -> None:
    """Despacha comandos exclusivos del administrador."""
    parts = full_text.strip().split()

    if cmd == "/start":
        diagnostic_mode.clear()
        send(ADMIN_ID, "▶️ *Bot reanudado para todos los usuarios.*")

    elif cmd == "/modo_diag":
        diagnostic_mode.set()
        send(ADMIN_ID, "🔧 *Modo diagnóstico activado.* Solo tú puedes usar el bot.")

    elif cmd == "/modo_normal":
        diagnostic_mode.clear()
        send(ADMIN_ID, "✅ *Modo normal restaurado.*")

    elif cmd == "/bloquear":
        if len(parts) == 2 and parts[1].isdigit():
            target = int(parts[1])
            block_user(target)
            send(ADMIN_ID, f"🚫 Usuario `{target}` bloqueado.")
            send(target, "🚫 Tu acceso al bot ha sido suspendido.")
        else:
            send(ADMIN_ID, "⚠️ Uso: /bloquear 123456789")

    elif cmd == "/desbloquear":
        if len(parts) == 2 and parts[1].isdigit():
            target = int(parts[1])
            unblock_user(target)
            send(ADMIN_ID, f"✅ Usuario `{target}` desbloqueado.")
            send(target, "✅ Tu acceso restaurado. Usa /inicio para empezar.")
        else:
            send(ADMIN_ID, "⚠️ Uso: /desbloquear 123456789")

    elif cmd == "/usuarios":
        approved = load_approved()
        blocked  = load_blocked()
        lista    = "\n".join(
            f"• `{u}` {'🚫' if u in blocked else '✅'}"
            for u in sorted(approved)
        )
        send(ADMIN_ID, f"👥 *Usuarios:*\n\n{lista}")

    elif cmd == "/status":
        from src.state import get_stats
        st  = get_stats(uid)
        cfg = get_config(uid)
        send(ADMIN_ID,
            f"📊 *Estado*\n\n"
            f"🔧 Diagnóstico: {'Activo' if diagnostic_mode.is_set() else 'Inactivo'}\n"
            f"📈 Barridos: {st.barridos_totales}\n"
            f"🎯 Matches: {st.matches_totales}\n"
            f"🔑 Keywords: `{', '.join(cfg.keywords.keys()) or '—'}`",
        )

    elif cmd == "/test":
        def _test_sweep():
            cfg_test = get_config(uid)
            result   = run_sweep(
                uid=uid,
                cfg=cfg_test,
                no_filter=True,
                limit=5,
                match_callback=lambda u, card: _match_callback(u, card, no_filter=True),
            )
            send(uid, f"🧪 Completado — Analizados: {result.analyzed} / Mostrados: {result.matched}")
        send(uid, "🧪 Test — 5 empleos sin filtros...")
        threading.Thread(target=_test_sweep, daemon=True).start()

    elif cmd == "/erase_history":
        delete_user_data(uid)
        send(uid, "🗑️ Historial y guardados borrados.")

    elif cmd == "/devcom":
        send(ADMIN_ID,
            "🔐 *Comandos Admin*\n\n"
            "/start          — reanuda el bot\n"
            "/stop           — detiene el bot\n"
            "/modo\\_diag     — modo diagnóstico\n"
            "/modo\\_normal   — modo normal\n"
            "/bloquear ID    — bloquea usuario\n"
            "/desbloquear ID — desbloquea usuario\n"
            "/usuarios       — lista usuarios\n"
            "/erase\\_history — borra historial\n"
            "/status         — estado del bot\n"
            "/test           — 5 empleos sin filtros\n"
            "/devcom         — este mensaje",
        )

    else:
        send(uid, "❓ Comando no reconocido.")


# ---------------------------------------------------------------------------
# Listener principal (long polling)
# ---------------------------------------------------------------------------

def listen() -> None:
    """
    Loop principal de long polling. Despacha updates a los handlers correspondientes.

    Maneja tres tipos de updates:
        - message con texto que empieza en '/': handle_command()
        - message con texto libre en paso activo de wizard: handle_text()
        - callback_query de botón inline: handle_callback()
    """
    log.info("Listener activo.")
    offset = 0

    while True:
        try:
            updates = get_updates(offset)

            for update in updates:
                offset = update["update_id"] + 1

                if "message" in update:
                    msg  = update["message"]
                    uid  = msg["from"]["id"]
                    name = msg["from"].get("first_name", str(uid))
                    text = msg.get("text", "")

                    if text.startswith("/"):
                        log.info(f"[{uid}] CMD: {text}")
                        handle_command(uid, text, name)
                    elif has_access(uid) and not diagnostic_mode.is_set():
                        paso = get_wizard(uid).paso
                        if paso in (
                            "pedir_cargo", "pedir_keyword",
                            "pedir_perfil_ia", "pedir_perfil_wizard",
                        ):
                            handle_text(uid, text)

                elif "callback_query" in update:
                    cb     = update["callback_query"]
                    uid    = cb["from"]["id"]
                    data   = cb.get("data", "")
                    msg_id = cb["message"]["message_id"]
                    answer_callback(cb["id"])

                    if diagnostic_mode.is_set() and uid != ADMIN_ID:
                        if not data.startswith(("aprobar_", "rechazar_")):
                            send(uid, "🔧 *Bot en mantenimiento.*")
                            continue

                    # Callbacks de vacantes — procesados directamente
                    if data.startswith("no_interesa_"):
                        delete_msg(uid, msg_id)

                    elif data.startswith("guardar_"):
                        job_id = data.split("_", 1)[1]
                        lines  = cb["message"].get("text", "").split("\n")
                        title  = lines[3].replace("📌 ", "").strip() if len(lines) > 3 else "Sin título"
                        link   = next(
                            (l for l in lines if l.startswith("🔗")), ""
                        ).replace("🔗 ", "").strip()
                        conf = save_to_record(uid, job_id, title, link)
                        delete_msg(uid, msg_id)
                        send(uid,
                            f"🔖 {conf}\n\n"
                            f"Máximo {MAX_RECORD_SIZE} ofertas guardadas.",
                        )

                    elif data.startswith("interesa_"):
                        clear_markup(uid, msg_id)

                    elif data.startswith(("aprobar_", "rechazar_")):
                        delete_msg(ADMIN_ID, msg_id)
                        handle_callback(ADMIN_ID, data, msg_id)

                    else:
                        handle_callback(uid, data, msg_id)

            time.sleep(2)

        except Exception as e:
            log.error(f"Listener error: {e}")
            time.sleep(5)
