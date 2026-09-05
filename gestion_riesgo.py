"""
gestion_riesgo.py — Bot Cripto (rediseño desde cero)
──────────────────────────────────────────────────────
SL fijo, trailing TP por pico, capital diario por interés compuesto.

Diseño confirmado (JJ_Cripto_Bot_Rediseno_BotCripto.docx):
  - SL: fijo -4%
  - Trailing TP (no confundir con SL): el tramo se fija por el PICO
    MÁXIMO histórico, nunca se relaja aunque el precio retroceda
      0% a 1%   -> SIN protección adicional (solo el SL fijo de -4%)
      1% a 3%   -> retrocede 50% desde el pico
      3% a 8%   -> retrocede 30% desde el pico
      > 8%      -> retrocede 20% desde el pico
  - Capital: recalculo diario 00:01 ARG (pospone si hay posiciones
    abiertas, reintenta cada 1 min), 5% del capital del día por
    posición, SIN reserva de ningún tipo
  - 6 posiciones simultáneas, máx 2 aperturas por ciclo de 15 min
  - SL/trailing: consulta DIRECTA a Pionex cada 2 seg (ver main.py,
    corre en threading.Thread aparte, patrón ya probado en v18)

  05/09 FIX: el breakeven activaba con CUALQUIER pico >0%, incluso un
  parpadeo de ruido normal de la oscilación del grid (0,1-0,2% es
  completamente normal, no es señal de nada) — esto causó cierres
  prematuros reales (GALAUSDT y TWTUSDT cerraron por "breakeven" casi
  al abrir, sin haber tenido de verdad una ganancia real). Corregido:
  ahora el pico necesita llegar a 1% (mismo umbral que PAXG) antes de
  que exista CUALQUIER protección más allá del SL fijo — por debajo de
  eso, solo el SL de -4% corre. Al llegar a 1%, cae directo en el
  primer tramo de trailing (retrocede 50% desde el pico), ya no existe
  una "zona de breakeven" separada en 0-1%.
"""
import db
import pionex_api

SL_FIJO_PCT = -4.0
MAX_POSICIONES_SIMULTANEAS = 6
MAX_APERTURAS_POR_CICLO = 2
PCT_CAPITAL_POR_OPERACION = 0.05  # 5% del capital del día
LEVERAGE_FIJO = 10
BREAKEVEN_ACTIVACION_MINIMA_PCT = 1.0  # 05/09: antes activaba con >0%, cambiado a pedido de Juanjo

# Tramos de trailing TP: (pico_desde, pico_hasta_o_None, retroceso_pct)
TRAMOS_TRAILING = [
    (0.0, 1.0, None),   # sin protección más allá del SL fijo (ver BREAKEVEN_ACTIVACION_MINIMA_PCT)
    (1.0, 3.0, 0.50),
    (3.0, 8.0, 0.30),
    (8.0, None, 0.20),
]


def calcular_tramo(pico_pct: float):
    """Devuelve (nombre_tramo, retroceso_pct_o_None) según el pico máximo histórico."""
    for desde, hasta, retroceso in TRAMOS_TRAILING:
        if hasta is None or pico_pct < hasta:
            if pico_pct >= desde:
                nombre = f"{desde}-{hasta or 'inf'}%"
                return nombre, retroceso
    return "0-1%", None


def evaluar_cierre(senal: dict, resultado_actual_pct: float) -> dict:
    """
    Decide si una posición abierta debe cerrarse AHORA, según SL fijo
    o trailing TP por pico. Se llama con el resultado ya consultado
    directo a Pionex (ver main.py — chequeo_rapido_riesgo).

    Devuelve {"cerrar": bool, "motivo": str|None}.
    """
    # 1. SL fijo — siempre se chequea primero, sin excepción
    if resultado_actual_pct <= SL_FIJO_PCT:
        return {"cerrar": True, "motivo": "stop_loss"}

    # 2. Actualizar pico máximo histórico (nunca baja)
    pico_actual = max(senal.get("pico_maximo_pct", 0) or 0, resultado_actual_pct)
    nombre_tramo, retroceso_pct = calcular_tramo(pico_actual)

    # 05/09: breakeven/trailing solo se activa si el pico llegó al menos
    # a BREAKEVEN_ACTIVACION_MINIMA_PCT (1%) — evita cerrar por ruido
    # normal del grid en picos chiquitos (ej. 0.1-0.2%).
    breakeven_activo = pico_actual >= BREAKEVEN_ACTIVACION_MINIMA_PCT

    db.actualizar_pico_y_tramo(senal["id"], pico_actual, nombre_tramo, breakeven_activo)

    # 3. Breakeven (pico entre 0 y 1%, sin trailing todavía): cierra si vuelve a <=0
    if not breakeven_activo:
        return {"cerrar": False, "motivo": None}

    if retroceso_pct is None:
        # Pico está justo en el límite exacto del breakeven (raro, borde) — tratar como breakeven
        if resultado_actual_pct <= 0:
            return {"cerrar": True, "motivo": "breakeven"}
        return {"cerrar": False, "motivo": None}

    # 4. Trailing activo: cierra si retrocedió más del % permitido desde el pico
    piso_permitido = pico_actual * (1 - retroceso_pct)
    if resultado_actual_pct <= piso_permitido:
        return {"cerrar": True, "motivo": "trailing_tp"}
    # Aun en tramo de trailing, nunca puede caer por debajo de 0% (breakeven es el piso absoluto)
    if resultado_actual_pct <= 0:
        return {"cerrar": True, "motivo": "breakeven"}

    return {"cerrar": False, "motivo": None}


def hay_lugar_para_abrir() -> dict:
    """Chequea el tope de 6 posiciones simultáneas y el tope de 2 aperturas por ciclo de 15min."""
    abiertas = db.contar_posiciones_abiertas()
    if abiertas >= MAX_POSICIONES_SIMULTANEAS:
        return {"hay_lugar": False, "motivo": f"tope de {MAX_POSICIONES_SIMULTANEAS} posiciones simultáneas"}

    aperturas_recientes = db.contar_aperturas_ultimos_minutos(15)
    if aperturas_recientes >= MAX_APERTURAS_POR_CICLO:
        return {"hay_lugar": False, "motivo": f"tope de {MAX_APERTURAS_POR_CICLO} aperturas cada 15 min"}

    return {"hay_lugar": True, "motivo": None}


def calcular_capital_por_operacion() -> float:
    """
    Capital del día ya fijado (00:01 ARG) × 5%. Si el recálculo diario
    todavía no corrió (posiciones abiertas a las 00:01), devuelve None
    — el llamador debe abstenerse de abrir hasta que haya un valor real.
    """
    cap = db.obtener_capital_diario()
    if not cap:
        return None
    return round(cap["capital_dia"] * PCT_CAPITAL_POR_OPERACION, 2)


def intentar_recalculo_diario(forzar: bool = False) -> str:
    """
    Recalcula el capital del día: 5% del balance REAL de Pionex.
    Se llama desde el scheduler a las 00:01 ARG, y reintenta cada 1 min
    si hay posiciones abiertas (no se puede confiar el balance con
    capital comprometido en grillas activas).
    Sin reserva de ningún tipo — si el capital bajó, las operaciones del
    día son más chicas en USD, sin excepción (decisión confirmada 03/09).
    """
    if not forzar and db.obtener_capital_diario():
        return None  # ya se recalculó hoy, no hacer nada

    if db.contar_posiciones_abiertas() > 0 and not forzar:
        return None  # pospuesto, hay posiciones abiertas — reintentar en 1 min

    try:
        balance = pionex_api.obtener_balance_cuenta()
    except Exception as e:
        return f"⚠️ No se pudo consultar el balance real de Pionex para el recálculo diario: {e}"

    if balance <= 0:
        return "⚠️ El balance consultado en Pionex fue 0 o inválido — recálculo diario NO aplicado, revisar manualmente."

    tamano_objetivo = round(balance * PCT_CAPITAL_POR_OPERACION, 2)
    db.guardar_capital_diario(balance, tamano_objetivo)
    return f"✅ Capital del día recalculado: USD {balance:.2f} — USD {tamano_objetivo:.2f} por operación (5%)."
