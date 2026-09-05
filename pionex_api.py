"""
pionex_api.py — Bot Cripto (rediseño desde cero)
──────────────────────────────────────────────────
Cliente para la API de Pionex — Futures Grid Bot.

Basado en la documentación oficial (mismo esquema de firma que v18,
confirmado funcionando en producción):
https://www.pionex.com/docs/api-docs/bot-api/futures-grid

REGLAS DE SEGURIDAD DE ESTE DISEÑO (confirmadas 01-04/09/2026):
1. MONTO MÍNIMO DINÁMICO — Pionex confirmó por soporte (01/09) que
   checkParams puede dar OK y create fallar después, porque el mínimo
   de inversión se recalcula según precio/leverage en el momento exacto.
   Por eso: SIEMPRE se llama a checkParams sin cachear, JUSTO antes de
   create, y el monto usado es el MAYOR entre el objetivo calculado y
   (mínimo dinámico + margen de seguridad 20-30%).
2. COMISIÓN vs. CANTIDAD DE GRILLAS — no existe API de Pionex que
   recomiende la cantidad de grillas. Se calcula con una fórmula propia
   (rango% / paso objetivo), pero el paso NUNCA puede ser menor a 3x la
   comisión ida+vuelta estimada — si no, se reduce la cantidad de
   grillas (escalones más anchos) para no perder plata en comisiones.
3. SL/TRAILING — consulta DIRECTA a Pionex (no cascada), cada 2 seg por
   posición. Se asumió el riesgo de HTTP 429 sin confirmar el límite
   real con Pionex (decisión de Juanjo, 01/09).

IMPORTANTE antes de producción:
- API Key con permiso de TRADE únicamente (sin retiro)
- Whitelist de IP con la IP saliente de Railway
- PIONEX_API_KEY y PIONEX_API_SECRET cargadas como Variables en Railway
  (mismos nombres que v18, confirmado por Juanjo)
"""
import os
import time
import hmac
import hashlib
import json
import threading
import requests

PIONEX_BASE_URL = "https://api.pionex.com"
PIONEX_API_KEY = os.environ.get("PIONEX_API_KEY", "")
PIONEX_API_SECRET = os.environ.get("PIONEX_API_SECRET", "")

# 04/09 — Pionex recomienda serializar las llamadas de ESCRITURA (crear/
# cerrar) por cuenta: con el ciclo de apertura (cada 15min) y el chequeo
# de cierre (cada 2seg) corriendo en hilos paralelos, hay riesgo real de
# condición de carrera si ambos escriben a la vez. Mismo fix ya aplicado
# en v18 el 27/08.
_pionex_write_lock = threading.Lock()

# Comisión real de Pionex Futuros: 0.02% maker / 0.05% taker (confirmado
# en múltiples fuentes públicas, Pionex no tiene endpoint propio de fees).
# Estimación conservadora ida+vuelta:
COMISION_IDA_VUELTA_PCT = 0.10

# Margen de seguridad sobre el mínimo dinámico de Pionex (nota oficial
# de soporte, 01/09/2026 — checkParams puede dar OK y create fallar
# después porque el mínimo cambia con el precio en el medio)
MARGEN_SOBRE_MINIMO_PCT = 0.25  # 25%, punto medio del rango 20-30% acordado


def _firmar(method: str, path: str, query: str, body: str = "") -> tuple:
    """
    Genera timestamp (ms) y firma HMAC-SHA256 según especificación de Pionex.
    GET           -> METHOD + PATH_URL + QUERY + TIMESTAMP
    POST / DELETE -> METHOD + PATH_URL + QUERY + TIMESTAMP + BODY
    """
    if not PIONEX_API_SECRET:
        raise RuntimeError("PIONEX_API_SECRET no configurada (falta variable en Railway).")

    timestamp = str(int(time.time() * 1000))
    query_completa = f"{query}&timestamp={timestamp}" if query else f"timestamp={timestamp}"
    payload = f"{method}{path}?{query_completa}"
    if method in ("POST", "DELETE"):
        payload += body

    firma = hmac.new(
        PIONEX_API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return timestamp, firma


def obtener_precision_par(par: str) -> int:
    """Consulta GET /common/symbols para la precisión de precio (decimales) de este par."""
    base = par.upper().replace("USDT", "").replace(".PERP", "")
    symbol = f"{base}_USDT_PERP"
    path = "/api/v1/common/symbols"
    query = f"symbols={symbol}&type=PERP"
    timestamp, firma = _firmar("GET", path, query)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?{query}&timestamp={timestamp}"
    try:
        resp = requests.get(url, headers=headers, timeout=10).json()
        symbols_list = resp.get("data", {}).get("symbols", [])
        if symbols_list:
            return int(symbols_list[0].get("quotePrecision", 4))
    except Exception:
        pass
    return 4


def _armar_body(par: str, top: float, bottom: float, row: int,
                 capital_usdt: float, leverage: int, trend: str = "long",
                 grid_type: str = "arithmetic", sl_pct: float = None) -> dict:
    base = par.upper().replace("USDT", "").replace(".PERP", "")
    precision = obtener_precision_par(base)
    bu_order_data = {
        "top": str(round(top, precision)),
        "bottom": str(round(bottom, precision)),
        "row": row,
        "grid_type": grid_type,
        "trend": trend,
        "leverage": leverage,
        "quoteInvestment": str(round(capital_usdt, 2)),
        "investmentFrom": "USER",
    }
    if sl_pct is not None:
        # 04/09 — SL NATIVO como respaldo de Pionex, además de nuestro
        # propio monitoreo activo (que sigue siendo el único que maneja
        # el trailing TP por pico — Pionex no puede hacer eso solo).
        # OJO con el formato: Pionex espera la FRACCIÓN DECIMAL (-0.04
        # para -4%), NO el número de porcentaje (-4.0) — bug real ya
        # encontrado y corregido en v18 el 29/08 (SL nativo se mandaba
        # como -1500% por no dividir entre 100). sl_pct llega en formato
        # "número de porcentaje" (ej. -4.0), se divide acá.
        bu_order_data["lossStopType"] = "profit_ratio"
        bu_order_data["lossStop"] = str(round(sl_pct / 100, 4))
    return {"base": f"{base}.PERP", "quote": "USDT", "buOrderData": bu_order_data}


def validar_parametros_grilla(par: str, top: float, bottom: float, row: int,
                               capital_usdt: float, leverage: int = 10,
                               trend: str = "long", grid_type: str = "arithmetic",
                               sl_pct: float = None) -> dict:
    """
    POST /futuresGrid/checkParams — NO crea orden real. Valida rango,
    capital mínimo/máximo. SIEMPRE llamar sin cachear, justo antes de
    crear_grilla_futuros_segura() — el mínimo cambia con el precio.
    """
    path = "/api/v1/bot/orders/futuresGrid/checkParams"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type, sl_pct)
    bod = body_dict["buOrderData"]
    bod_snake = {
        "top": bod["top"], "bottom": bod["bottom"], "row": bod["row"],
        "grid_type": bod["grid_type"], "trend": bod["trend"], "leverage": bod["leverage"],
        "quote_investment": bod["quoteInvestment"], "extra_margin": False,
    }
    if "lossStop" in bod:
        bod_snake["loss_stop_type"] = bod["lossStopType"]
        bod_snake["loss_stop"] = bod["lossStop"]
    body_dict["buOrderData"] = bod_snake
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma, "Content-Type": "application/json"}
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    resp = requests.post(url, headers=headers, data=body_json, timeout=15)
    return resp.json()




def _extraer_minimo_del_error(resp: dict):
    """
    Intenta extraer el monto mínimo dinámico del mensaje de error de
    checkParams (Pionex lo incluye en texto, ej: "...minimum is 17.69...").
    Devuelve None si no se pudo extraer (el llamador debe manejar ese caso).
    """
    import re
    mensaje = str(resp.get("message") or resp.get("data") or resp)
    match = re.search(r"(\d+\.?\d*)", mensaje)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def crear_grilla_futuros_segura(par: str, top: float, bottom: float, row: int,
                                 capital_objetivo_usdt: float, leverage: int,
                                 trend: str = "long", grid_type: str = "arithmetic",
                                 sl_pct: float = None, max_reintentos: int = 2) -> dict:
    """
    Apertura REAL con la verificación de mínimo dinámico incluida:
    1. checkParams sin cachear con el monto objetivo
    2. Si Pionex rechaza por mínimo, extrae el mínimo real del error,
       reintenta con mínimo + margen de seguridad (25%)
    3. Solo si checkParams pasa, se llama a create

    sl_pct: SL nativo de respaldo (además de nuestro monitoreo activo),
    formato "número de porcentaje" (ej. -4.0). None = sin SL nativo.

    Devuelve dict con "ok": bool, "resultado": respuesta cruda de Pionex,
    "capital_usado": el monto final que se intentó usar.
    """
    capital_actual = capital_objetivo_usdt
    intentos = 0
    while intentos <= max_reintentos:
        intentos += 1
        check = validar_parametros_grilla(par, top, bottom, row, capital_actual, leverage, trend, grid_type, sl_pct)
        if check.get("result") is True or check.get("code") == 0 or check.get("data"):
            # checkParams OK -> crear de verdad, INMEDIATAMENTE (sin demora que permita que el mínimo vuelva a moverse)
            resultado = crear_grilla_futuros(par, top, bottom, row, capital_actual, leverage, trend, grid_type, sl_pct)
            ok = resultado.get("result") is True or resultado.get("code") == 0
            return {"ok": ok, "resultado": resultado, "capital_usado": capital_actual, "intentos": intentos}

        # checkParams falló -> ¿es por monto mínimo? intentar extraer y reintentar con margen
        minimo_extraido = _extraer_minimo_del_error(check)
        if minimo_extraido and minimo_extraido > capital_actual:
            capital_actual = round(minimo_extraido * (1 + MARGEN_SOBRE_MINIMO_PCT), 2)
            continue  # reintenta con el monto corregido
        # Falló por otro motivo, o no se pudo extraer el mínimo -> no insistir a ciegas
        return {"ok": False, "resultado": check, "capital_usado": capital_actual, "intentos": intentos}

    return {"ok": False, "resultado": check, "capital_usado": capital_actual, "intentos": intentos}


def crear_grilla_futuros(par: str, top: float, bottom: float, row: int,
                          capital_usdt: float, leverage: int = 10,
                          trend: str = "long", grid_type: str = "arithmetic",
                          sl_pct: float = None) -> dict:
    """POST /futuresGrid/create — crea una grilla REAL. Usar vía crear_grilla_futuros_segura(), no directo."""
    path = "/api/v1/bot/orders/futuresGrid/create"
    body_dict = _armar_body(par, top, bottom, row, capital_usdt, leverage, trend, grid_type, sl_pct)
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma, "Content-Type": "application/json"}
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    with _pionex_write_lock:
        resp = requests.post(url, headers=headers, data=body_json, timeout=15)
        return resp.json()


def consultar_orden(bu_order_id: str) -> dict:
    """GET /futuresGrid/order — estado completo: resultado, liquidationPrice, status, etc."""
    path = "/api/v1/bot/orders/futuresGrid/order"
    query = f"buOrderId={bu_order_id}"
    timestamp, firma = _firmar("GET", path, query)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?{query}&timestamp={timestamp}"
    resp = requests.get(url, headers=headers, timeout=15)
    return resp.json()


def calcular_resultado_actual(bu_order_id: str, precio_actual: float):
    """
    % de resultado REAL de una posición abierta ahora mismo.

    04/09 — FIX CRÍTICO (repite un bug ya corregido en v16 el 07/08, que
    yo mismo reintroduje sin darme cuenta porque copié el pionex_api.py
    de referencia del proyecto, que resultó ser la versión DE ANTES del
    fix): usar solo marginBalance NO alcanza — no refleja en tiempo real
    la ganancia/pérdida NO REALIZADA de la posición que el grid ya
    compró (baseAmount). Caso real que lo confirmó: ONDOUSDT cerró
    manualmente en -4,91% real (visto en la app de Pionex) mientras nuestro
    cálculo (solo marginBalance) nunca bajó de -4% — el SL nunca se
    disparó a tiempo, exactamente el mismo patrón del bug de INJUSDT (v16).

    Fórmula correcta (validada entonces contra 3 números reales de INJ):
      resultado% = (marginBalance - initUsdtInvestment) / quoteInvestment * 100
                   + baseAmount * (precio_actual - positionOpenPrice) / quoteInvestment * 100
    baseAmount ya viene con signo (negativo en CORTO), así que la fórmula
    es la misma para LARGO y CORTO sin necesidad de una rama aparte.

    precio_actual: se pasa desde afuera (cascada externa, gratis) para no
    sumar una consulta directa más a Pionex solo para esto.
    """
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}
    try:
        margin_balance = float(bod.get("marginBalance", 0) or 0)
        init_investment = float(bod.get("initUsdtInvestment", 0) or 0)
        quote_investment = float(bod.get("quoteInvestment") or bod.get("initQuoteInvestment") or 0)
        if quote_investment <= 0:
            return None

        base_amount = float(bod.get("baseAmount", 0) or 0)
        position_open_price = float(bod.get("positionOpenPrice", 0) or 0)
        no_realizado_usd = base_amount * (precio_actual - position_open_price) if position_open_price > 0 else 0.0

        resultado_base_pct = (margin_balance - init_investment) / quote_investment * 100
        resultado_no_realizado_pct = no_realizado_usd / quote_investment * 100
        return round(resultado_base_pct + resultado_no_realizado_pct, 4)
    except (ValueError, TypeError):
        return None


def esta_cerrada(bu_order_id: str) -> dict:
    """Detecta si una grilla ya cerró (TP, cancelación, liquidación) y su resultado real."""
    data = consultar_orden(bu_order_id).get("data", {}) or {}
    bod = data.get("buOrderData", {}) or {}
    status_top = (data.get("status") or "").lower()
    status_bod = (bod.get("status") or "").lower()
    reason = bod.get("reasonBy")

    cerrada = status_top in ("finished", "closed", "cancelled", "canceled") or \
              status_bod in ("finished", "closed", "cancelled", "canceled", "stopped")

    resultado_pct = None
    if cerrada:
        try:
            margin_balance = float(bod.get("marginBalance", 0) or 0)
            init_investment = float(bod.get("initUsdtInvestment", 0) or 0)
            quote_investment = float(bod.get("quoteInvestment") or bod.get("initQuoteInvestment") or 0)
            if quote_investment > 0:
                resultado_pct = round((margin_balance - init_investment) / quote_investment * 100, 4)
        except (ValueError, TypeError):
            resultado_pct = None

    return {"cerrada": cerrada, "motivo": reason, "resultado_pct": resultado_pct}


def cerrar_grilla_futuros(bu_order_id: str, nota: str = "Cierre por SL/trailing") -> dict:
    """
    POST /futuresGrid/cancel — cierra una grilla YA ABIERTA.

    04/09 — FIX CRÍTICO (mismo bug ya corregido en v18 el 19/08, que
    reintroduje al escribir esto desde cero sin verificar contra el
    historial): se sacó "immediate": True — según la documentación
    oficial, ese flag es una recuperación especial SOLO válida cuando la
    orden ya está en estado close_position con un límite TP/SL trabado
    sin llenar. En cualquier posición corriendo normalmente (nuestro
    caso siempre), Pionex lo RECHAZA con "Forbidden: invalid status" —
    y como antes no se chequeaba el resultado, ese rechazo quedaba
    invisible: la posición se marcaba "cerrada" en nuestra base
    (dejando de monitorearla) mientras seguía corriendo real en Pionex
    sin nadie vigilándola. Esto explica el patrón real visto el 04/09:
    4 de 6 posiciones cerraron con pérdidas superiores al 5% pese al SL
    fijo de -4% — el SL se disparaba a tiempo, pero el cierre real
    fallaba en silencio y la posición seguía cayendo sin control.

    Ahora SIEMPRE devuelve {"ok": bool, "resultado": ...} — el llamador
    (main.py) NUNCA debe marcar una posición como cerrada en nuestra
    base si "ok" es False.
    """
    path = "/api/v1/bot/orders/futuresGrid/cancel"
    body_dict = {
        "buOrderId": bu_order_id,
        "closeNote": nota,
        "closeSellModel": "TO_QUOTE",
        "closeSlippage": "0.01",
    }
    body_json = json.dumps(body_dict, separators=(",", ":"))
    timestamp, firma = _firmar("POST", path, "", body_json)
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma, "Content-Type": "application/json"}
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    try:
        with _pionex_write_lock:
            resp = requests.post(url, headers=headers, data=body_json, timeout=15)
            data = resp.json()
        ok = bool(data.get("result"))
        if not ok:
            print(f"⚠️ cerrar_grilla_futuros: Pionex RECHAZÓ el cierre de {bu_order_id}: {str(data)[:300]}")
        return {"ok": ok, "resultado": data}
    except Exception as e:
        print(f"⚠️ cerrar_grilla_futuros: error de conexión al cerrar {bu_order_id}: {e}")
        return {"ok": False, "resultado": str(e)}


def listar_grillas_abiertas() -> list:
    """
    GET /bot/orders — lista TODAS las grillas reales abiertas en la
    cuenta. Usado por el chequeo de huérfanas (cada 30 min).

    04/09 (bug real detectado en producción): usaba el endpoint
    /futuresGrid que no existe/no filtra bien — corregido al endpoint
    confirmado en v16/v18: /api/v1/bot/orders, SIN type= en la query
    (eso causa INVALID_SIGNATURE), filtrando client-side por
    buOrderType=="futures_grid" y status running/trading. El campo real
    de la respuesta es "results", no "orders" como se había asumido
    antes sin confirmar.
    Devuelve una LISTA ya filtrada (no el dict crudo).
    """
    path = "/api/v1/bot/orders"
    timestamp, firma = _firmar("GET", path, "")
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        data = resp.json()
        if not data.get("result"):
            print(f"⚠️ listar_grillas_abiertas: Pionex respondió result=false: {str(data)[:200]}")
            return []
        ordenes = data.get("data", {}).get("results", [])
        return [
            o for o in ordenes
            if o.get("buOrderType") == "futures_grid"
            and str(o.get("status", "")).lower() in ("running", "trading")
        ]
    except Exception as e:
        print(f"⚠️ listar_grillas_abiertas: error de conexión: {e}")
        return []


def obtener_balance_cuenta() -> float:
    """
    GET /account/balances — balance real de la cuenta, SOLO en USDT.
    Usado para el recálculo diario de capital de BOT CRIPTO (interés
    compuesto). Aislamiento de capital entre cinturones: Juanjo mantiene
    fondos en USDT (Bot Cripto) y en BTC (PAXG) en la misma cuenta de
    Pionex — esta función filtra explícitamente coin=="USDT" y NUNCA
    toca el balance en BTC, que es capital exclusivo de PAXG (04/09).
    """
    path = "/api/v1/account/balances"
    timestamp, firma = _firmar("GET", path, "")
    headers = {"PIONEX-KEY": PIONEX_API_KEY, "PIONEX-SIGNATURE": firma}
    url = f"{PIONEX_BASE_URL}{path}?timestamp={timestamp}"
    resp = requests.get(url, headers=headers, timeout=15).json()
    balances = resp.get("data", {}).get("balances", [])
    for b in balances:
        if b.get("coin") == "USDT":
            return float(b.get("free", 0) or 0) + float(b.get("frozen", 0) or 0)
    return 0.0
