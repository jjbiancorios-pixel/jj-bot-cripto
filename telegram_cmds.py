"""
telegram_cmds.py — Bot Cripto (rediseño desde cero)
──────────────────────────────────────────────────────
Versión nueva, simplificada — sin PAXG/BingX/martingala (viven en sus
propios procesos separados). Reutiliza @JJ_Cripto_Bot (mismo token).

Comandos:
  /estado        — resumen de hoy (posiciones abiertas, cerradas, win rate)
  /pendientes    — posiciones abiertas ahora mismo
  /capital       — capital del día (interés compuesto)
  /pausar_todo [motivo] / /reanudar_todo
  /probar_pionex PAR PRECIO [LEVERAGE] [CAPITAL]
  /backup_db     — manda el archivo completo de la base por Telegram
  /gates PAR     — últimos chequeos de gates para un par (diagnóstico)
  /ayuda
"""
import requests
import os
from datetime import datetime
import db

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

_ultimo_update_id = 0


def inicializar_offset_telegram():
    """Descarta el backlog viejo de mensajes al arrancar (mismo fix crítico de v18, 16/08)."""
    global _ultimo_update_id
    intentos = 0
    while intentos < 50:
        intentos += 1
        data = _api("getUpdates", offset=_ultimo_update_id + 1, timeout=1)
        if not data.get("ok"):
            break
        updates = data.get("result", [])
        if not updates:
            break
        _ultimo_update_id = max(u["update_id"] for u in updates)
    print(f"📡 Telegram: listo, escuchando desde update_id={_ultimo_update_id}.")


def _api(method: str, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    try:
        r = requests.post(url, json=params, timeout=20)
        return r.json()
    except Exception as e:
        print(f"Telegram API error ({method}): {e}")
        return {}


def enviar(msg: str):
    _api("sendMessage", chat_id=CHAT_ID, text=msg, parse_mode="HTML")


def _parse_float(s):
    try:
        return float(s.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _cmd_estado() -> str:
    r = db.resumen_diario()
    cap = db.obtener_capital_diario()
    lineas = [f"📊 <b>Estado de hoy</b> ({r['fecha']})"]
    lineas.append(f"Cerradas: {r['n_cerradas']} | Abiertas: {r['n_abiertas']}")
    if r["n_cerradas"] > 0:
        lineas.append(f"✅ {r['n_ganadoras']} ganadoras | ❌ {r['n_perdedoras']} perdedoras")
        lineas.append(f"Win rate: {r['win_rate_pct']}% | Resultado prom: {r['resultado_prom_pct']:+.2f}%")
    if cap:
        lineas.append(f"\n💰 Capital del día: USD {cap['capital_dia']:.2f} | Por operación: USD {cap['tamano_objetivo']:.2f}")
    else:
        lineas.append("\n⚠️ Capital del día todavía no se recalculó (puede estar pospuesto por posiciones abiertas a las 00:01).")
    return "\n".join(lineas)


def _cmd_pendientes() -> str:
    abiertas = db.posiciones_abiertas()
    if not abiertas:
        return "✅ No hay posiciones abiertas ahora mismo."
    lineas = [f"📋 <b>Posiciones abiertas</b> ({len(abiertas)}/6):"]
    for p in abiertas:
        pico = p.get("pico_maximo_pct") or 0
        tramo = p.get("tramo_trailing_actual") or "sin pico todavía"
        lineas.append(f"#{p['id']} {p['par']} {p['direccion']} | pico {pico:+.2f}% | tramo: {tramo}")
    return "\n".join(lineas)


def _cmd_capital() -> str:
    cap = db.obtener_capital_diario()
    if not cap:
        return "💰 Todavía no corrió el recálculo de hoy (pospuesto si hay posiciones abiertas a las 00:01)."
    return (
        f"💰 <b>Capital del día</b> ({cap['fecha']})\n"
        f"Capital real: USD {cap['capital_dia']:.2f}\n"
        f"Por operación (5%): USD {cap['tamano_objetivo']:.2f}"
    )


def _cmd_pausar_todo(args: list) -> str:
    motivo = " ".join(args) if args else "sin motivo especificado"
    db.pausar_todo(motivo)
    return f"🛑 <b>Bot pausado.</b> Motivo: {motivo}\nLas posiciones ya abiertas siguen monitoreadas normalmente (SL/trailing no se pausan)."


def _cmd_reanudar_todo() -> str:
    db.reanudar_todo()
    return "✅ Bot reanudado — vuelve a analizar y abrir posiciones normalmente."


def _cmd_probar_pionex(args: list) -> str:
    if len(args) < 2:
        return "Uso: /probar_pionex PAR PRECIO [LEVERAGE] [CAPITAL]\nEj: /probar_pionex BTC 63000"
    par = args[0].upper().strip().replace("USDT", "")
    precio = _parse_float(args[1])
    if precio is None:
        return "⚠️ El precio tiene que ser un número."
    leverage = int(_parse_float(args[2])) if len(args) > 2 and _parse_float(args[2]) else 10
    capital = _parse_float(args[3]) if len(args) > 3 and _parse_float(args[3]) else 50
    top = round(precio * 1.03, 6)
    bottom = round(precio * 0.97, 6)
    try:
        import pionex_api
        resultado = pionex_api.validar_parametros_grilla(par, top, bottom, 67, capital, leverage)
        return f"🧪 <b>Prueba Pionex — {par}</b>\n<code>{resultado}</code>"
    except Exception as e:
        return f"⚠️ Error al conectar con Pionex: {e}"


def _cmd_backup_db() -> str:
    if not os.path.exists(db.DB_PATH):
        return "⚠️ No encontré la base de datos en el servidor."
    try:
        with open(db.DB_PATH, "rb") as f:
            contenido = f.read()
    except Exception as e:
        return f"⚠️ No pude leer el archivo: {e}"
    fecha = datetime.now(db.TZ_ARG).strftime("%Y%m%d_%H%M")
    nombre_archivo = f"bot_cripto_backup_{fecha}.db"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    files = {"document": (nombre_archivo, contenido, "application/x-sqlite3")}
    data = {"chat_id": CHAT_ID, "caption": f"💾 Backup — {len(contenido)/1024:.1f} KB"}
    try:
        r = requests.post(url, data=data, files=files, timeout=60)
        if not r.json().get("ok"):
            return f"⚠️ Falló el envío: {r.json()}"
        return None
    except Exception as e:
        return f"⚠️ Error: {e}"


def _cmd_debug_orden(args: list) -> str:
    """
    04/09 — Diagnóstico: muestra la respuesta CRUDA de Pionex para la
    posición de un par (abierta O ya cerrada — busca en TODO el
    historial, no solo abiertas, para poder investigar cierres
    inesperados después de que ya pasaron).
    Uso: /debug_orden PAR
    """
    if not args:
        return "Uso: /debug_orden PAR\nEj: /debug_orden TAO"
    par = args[0].upper().strip()
    if not par.endswith("USDT"):
        par += "USDT"

    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM senales WHERE par = ? AND bu_order_id IS NOT NULL ORDER BY id DESC LIMIT 1", (par,))
    row = cur.fetchone()
    conn.close()
    senal = dict(row) if row else None

    if not senal or not senal.get("bu_order_id"):
        return f"⚠️ No encontré ninguna posición de {par} con bu_order_id en el historial."

    try:
        import pionex_api
        resultado = pionex_api.consultar_orden(senal["bu_order_id"])
        estado_local = "cerrada en nuestra base" if senal["cerrado"] else "abierta en nuestra base"
        return (
            f"🔍 <b>Debug — {par}</b> ({estado_local}, motivo_cierre local: {senal.get('motivo_cierre') or '—'})\n"
            f"bu_order_id: {senal['bu_order_id']}\n\n<code>{resultado}</code>"
        )
    except Exception as e:
        return f"⚠️ Error: {e}"


def _cmd_cerrar_manual(args: list) -> str:
    """
    04/09 — Corrige manualmente una posición que en realidad YA está
    cerrada en Pionex (cerrada a mano por Juanjo, o por cualquier motivo
    fuera del flujo automático) pero nuestra base todavía la muestra
    como abierta — para que deje de aparecer en /pendientes y deje de
    disparar la alerta de huérfana cada 30 min.
    Uso: /cerrar_manual PAR RESULTADO_PCT
    Ej: /cerrar_manual TAO -0.66
    """
    if len(args) < 2:
        return "Uso: /cerrar_manual PAR RESULTADO_PCT\nEj: /cerrar_manual TAO -0.66"
    par = args[0].upper().strip()
    if not par.endswith("USDT"):
        par += "USDT"
    resultado = _parse_float(args[1])
    if resultado is None:
        return "⚠️ Resultado inválido. Usá un número, ej: -0.66 o +2.43"

    abiertas = db.posiciones_abiertas()
    senal = next((s for s in abiertas if s["par"] == par), None)
    if not senal:
        return f"⚠️ No encontré {par} entre las posiciones abiertas en nuestra base."

    db.cerrar_senal(senal["id"], resultado, "cerrado_manual")
    return f"✅ {par} (id {senal['id']}) marcado como cerrado en nuestra base — resultado {resultado:+.2f}%. Ya no debería aparecer en /pendientes ni como huérfana."


def _cmd_gates(args: list) -> str:
    if not args:
        return "Uso: /gates PAR\nEj: /gates BTC"
    par = args[0].upper().strip()
    if not par.endswith("USDT"):
        par += "USDT"
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM gates_log WHERE par = ? ORDER BY id DESC LIMIT 10", (par,))
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not filas:
        return f"Sin registros de gates todavía para {par}."
    lineas = [f"🔍 <b>Últimos chequeos — {par}</b>"]
    for f in filas:
        gates = f"ADX:{'✅' if f['paso_adx'] else '❌'} EMA4h:{'✅' if f['paso_ema4h'] else '❌'} Funding:{'✅' if f['paso_funding'] else '❌'}"
        lineas.append(f"{f['fecha']} {f['hora']} | {gates} | score {f['score']} (momentum {f['score_momentum']}) | {'CALIFICÓ' if f['califico'] else 'no calificó'}")
    return "\n".join(lineas)


def procesar_comando(texto: str) -> str:
    partes = texto.strip().split()
    if not partes:
        return ""
    cmd = partes[0].lower()
    args = partes[1:]

    if cmd == "/estado":
        return _cmd_estado()
    elif cmd == "/pendientes":
        return _cmd_pendientes()
    elif cmd == "/capital":
        return _cmd_capital()
    elif cmd == "/pausar_todo":
        return _cmd_pausar_todo(args)
    elif cmd == "/reanudar_todo":
        return _cmd_reanudar_todo()
    elif cmd == "/probar_pionex":
        return _cmd_probar_pionex(args)
    elif cmd == "/backup_db":
        return _cmd_backup_db()
    elif cmd == "/gates":
        return _cmd_gates(args)
    elif cmd == "/debug_orden":
        return _cmd_debug_orden(args)
    elif cmd == "/cerrar_manual":
        return _cmd_cerrar_manual(args)
    elif cmd in ("/ayuda", "/help", "/start"):
        return (
            "🤖 <b>Bot Cripto v2 — Comandos</b>\n\n"
            "/estado — resumen de hoy (posiciones, win rate, capital)\n"
            "/pendientes — posiciones abiertas ahora, con pico y tramo de trailing\n"
            "/capital — capital del día (interés compuesto)\n"
            "/gates PAR — últimos 10 chequeos de gates para un par (diagnóstico)\n"
            "/debug_orden PAR — respuesta cruda de Pionex para una posición (diagnóstico)\n"
            "/cerrar_manual PAR RESULTADO_PCT — corrige una posición ya cerrada por vos "
            "que nuestra base sigue mostrando abierta (ej: /cerrar_manual TAO -0.66)\n"
            "/pausar_todo [motivo] — frena aperturas nuevas (SL/trailing sigue activo)\n"
            "/reanudar_todo\n"
            "/probar_pionex PAR PRECIO — prueba conexión sin crear orden real\n"
            "/backup_db — manda la base de datos completa por Telegram"
        )
    return f"⚠️ No reconozco el comando \"{cmd}\" — mandá /ayuda para ver la lista."


def revisar_updates():
    global _ultimo_update_id
    data = _api("getUpdates", offset=_ultimo_update_id + 1, timeout=5)
    if not data.get("ok"):
        return
    for update in data.get("result", []):
        _ultimo_update_id = max(_ultimo_update_id, update["update_id"])
        msg = update.get("message", {})
        texto = msg.get("text", "")
        chat_id_msg = str(msg.get("chat", {}).get("id", ""))
        if not texto.startswith("/"):
            continue
        if CHAT_ID and chat_id_msg != str(CHAT_ID):
            continue
        respuesta = procesar_comando(texto)
        if respuesta:
            enviar(respuesta)
