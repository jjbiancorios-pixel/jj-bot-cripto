"""
gestion_riesgo.py — Bot Cripto (rediseño desde cero)
──────────────────────────────────────────────────────
SL fijo, trailing TP por pico, capital diario por interés compuesto.

  05/09 FIX (investigación con evidencia real, pedido de Juanjo tras ver
  12 operaciones —la mayoría cerrando en breakeven con ganancias <0.3%—
  durante el período sin monitoreo): un umbral de breakeven FIJO (+1%
  para todas las monedas por igual) causa "whipsaw" — cierra por ruido
  normal antes de que la operación tenga margen real de desarrollarse.
  Evidencia (búsqueda 05/09): backtest de +10.000 operaciones cripto
  mostró que un trailing ajustado (3%) bajó el win rate de 67.9% a
  58.2% frente a un SL fijo más ancho, por whipsaw. Recomendación
  consistente en múltiples fuentes: umbrales basados en ATR (volatilidad
  real de cada moneda), no un % fijo igual para todas.
  Ahora el breakeven y los tramos de trailing escalan con el ATR% que
  cada posición ya tiene guardado (senal["atr_pct"]) — monedas más
  volátiles reciben más margen antes de que el breakeven las corte,
  monedas tranquilas mantienen protección más ajustada.
"""
import db
import pionex_api
from datetime import datetime
from db import TZ_ARG

SL_FIJO_PCT = -7.5  # 06/09: ancho de -4% a -7.5% — dejar que la grilla se desarrolle más
MAX_POSICIONES_SIMULTANEAS = 6
MAX_APERTURAS_POR_CICLO = 2
PCT_CAPITAL_POR_OPERACION = 0.05  # 5% del capital del día
LEVERAGE_FIJO = 10

# 06/09: escalados junto con el nuevo SL más ancho (-7.5%, antes -4%) —
# mismo criterio proporcional, para que el trailing no quede corto
# frente a un SL mucho más ancho (eso ya vimos que empeora la relación
# riesgo/beneficio: ganadoras chicas de ~0.7% vs. pérdidas de ~-4.3%).
BREAKEVEN_ATR_MULT = 2.5
BREAKEVEN_PISO_PCT = 2.0
TRAMO2_ATR_MULT = 4.5   # sin cambios
TRAMO2_PISO_PCT = 5.5
TRAMO3_ATR_MULT = 9.0   # sin cambios
TRAMO3_PISO_PCT = 15.0


def _umbrales_por_atr(atr_pct: float):
    """Calcula los 3 quiebres de tramo (breakeven, tramo2, tramo3) escalados por ATR, con piso mínimo."""
    atr_pct = atr_pct or 0
    u1 = max(BREAKEVEN_PISO_PCT, atr_pct * BREAKEVEN_ATR_MULT)
    u2 = max(TRAMO2_PISO_PCT, atr_pct * TRAMO2_ATR_MULT)
    u3 = max(TRAMO3_PISO_PCT, atr_pct * TRAMO3_ATR_MULT)
    return u1, u2, u3


def calcular_tramo(pico_pct: float, atr_pct: float = None):
    """Devuelve (nombre_tramo, retroceso_pct_o_None) según el pico máximo histórico, escalado por ATR."""
    u1, u2, u3 = _umbrales_por_atr(atr_pct)
    tramos = [
        (0.0, u1, None),
        (u1, u2, 0.35),  # 06/09: bajado de 0.50 a 0.35 — deja correr más antes de asegurar
        (u2, u3, 0.30),
        (u3, None, 0.20),
    ]
    for desde, hasta, retroceso in tramos:
        if hasta is None or pico_pct < hasta:
            if pico_pct >= desde:
                nombre = f"{round(desde,2)}-{round(hasta,2) if hasta else 'inf'}%"
                return nombre, retroceso
    return f"0-{round(u1,2)}%", None


HORAS_MAX_FUERA_DE_RANGO = 3  # 10/09: cierre forzado si lleva más de esto fuera del rango de la grilla


def evaluar_cierre(senal: dict, resultado_actual_pct: float, precio_actual: float = None, btc_estado: str = None) -> dict:
    """
    Decide si una posición abierta debe cerrarse AHORA, según SL fijo
    o trailing TP por pico (umbrales escalados por el ATR de la moneda).
    Se llama con el resultado ya consultado directo a Pionex.

    10/09 — 2 mecanismos nuevos, ambos SOLO si ya cruzó breakeven (nunca
    aumentan el riesgo de una posición que sigue en pérdida sin haber
    ganado nunca — eso sigue protegido solo por el SL fijo):
    1. Fuera de rango: si el precio actual quedó fuera del rango de la
       grilla (Pionex "pausa el arbitraje"), el retroceso permitido se
       reduce a la MITAD mientras dure — y si pasan 3hs seguidas fuera
       de rango, se fuerza el cierre igual, tenga el resultado que tenga.
    2. BTC en contra: si BTC cambió de tendencia y quedó en contra de la
       dirección de esta posición, mismo efecto (retroceso a la mitad,
       sin cierre forzado por tiempo — solo aplica el punto 1 a eso).
    Las 2 condiciones NO se suman: si ambas se dan a la vez, el
    retroceso queda igual de reducido (a la mitad), no más.

    Devuelve {"cerrar": bool, "motivo": str|None}.
    """
    if resultado_actual_pct <= SL_FIJO_PCT:
        return {"cerrar": True, "motivo": "stop_loss"}

    atr_pct = senal.get("atr_pct")
    pico_actual = max(senal.get("pico_maximo_pct", 0) or 0, resultado_actual_pct)
    nombre_tramo, retroceso_pct = calcular_tramo(pico_actual, atr_pct)

    umbral_breakeven, _, _ = _umbrales_por_atr(atr_pct)
    breakeven_activo = pico_actual >= umbral_breakeven

    db.actualizar_pico_y_tramo(senal["id"], pico_actual, nombre_tramo, breakeven_activo)

    if not breakeven_activo:
        db.actualizar_fuera_rango(senal["id"], None)  # nunca cruzó breakeven, no aplica ninguno de los 2 mecanismos
        return {"cerrar": False, "motivo": None}

    # ── Mecanismo 1: fuera de rango (solo acá abajo, ya con breakeven activo) ──
    fuera_de_rango = False
    if precio_actual is not None and senal.get("rango_bajo") and senal.get("rango_alto"):
        fuera_de_rango = precio_actual < senal["rango_bajo"] or precio_actual > senal["rango_alto"]

    fuera_rango_desde = senal.get("fuera_rango_desde")
    if fuera_de_rango and not fuera_rango_desde:
        fuera_rango_desde = datetime.now(TZ_ARG).isoformat()
        db.actualizar_fuera_rango(senal["id"], fuera_rango_desde)
    elif not fuera_de_rango and fuera_rango_desde:
        fuera_rango_desde = None
        db.actualizar_fuera_rango(senal["id"], None)

    if fuera_rango_desde:
        try:
            desde_dt = datetime.fromisoformat(fuera_rango_desde)
            horas_fuera = (datetime.now(TZ_ARG) - desde_dt).total_seconds() / 3600
            if horas_fuera >= HORAS_MAX_FUERA_DE_RANGO:
                return {"cerrar": True, "motivo": "fuera_rango_3hs"}
        except Exception:
            pass

    # ── Mecanismo 2: BTC en contra ──
    direccion = senal.get("direccion")
    btc_en_contra = bool(btc_estado) and (
        (direccion == "LARGO" and btc_estado == "BAJISTA") or
        (direccion == "CORTO" and btc_estado == "ALCISTA")
    )

    modo_cauto = fuera_de_rango or btc_en_contra

    if retroceso_pct is None:
        if resultado_actual_pct <= 0:
            return {"cerrar": True, "motivo": "breakeven"}
        return {"cerrar": False, "motivo": None}

    retroceso_efectivo = retroceso_pct / 2 if modo_cauto else retroceso_pct
    piso_permitido = pico_actual * (1 - retroceso_efectivo)
    if resultado_actual_pct <= piso_permitido:
        motivo = "trailing_tp_cauto" if modo_cauto else "trailing_tp"
        return {"cerrar": True, "motivo": motivo}
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
