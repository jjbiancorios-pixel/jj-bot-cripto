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


def evaluar_cierre_simulado(direccion: str, atr_pct: float, pico_maximo_pct: float, resultado_actual_pct: float) -> dict:
    """
    13/09 — Versión SIN efectos en la base (no escribe en `senales`,
    para usar con simulaciones sin capital real). Misma lógica central
    que evaluar_cierre (SL fijo + trailing por ATR), sin los mecanismos
    de "fuera de rango" ni "BTC en contra" (simplificación consciente,
    esos 2 dependen de datos específicos de la grilla real).
    """
    if resultado_actual_pct <= SL_FIJO_PCT:
        return {"cerrar": True, "motivo": "stop_loss", "pico_nuevo": pico_maximo_pct}

    pico_actual = max(pico_maximo_pct or 0, resultado_actual_pct)
    nombre_tramo, retroceso_pct = calcular_tramo(pico_actual, atr_pct)
    umbral_breakeven, _, _ = _umbrales_por_atr(atr_pct)
    breakeven_activo = pico_actual >= umbral_breakeven

    if not breakeven_activo:
        return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual}

    if retroceso_pct is None:
        if resultado_actual_pct <= 0:
            return {"cerrar": True, "motivo": "breakeven", "pico_nuevo": pico_actual}
        return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual}

    piso_permitido = pico_actual * (1 - retroceso_pct)
    if resultado_actual_pct <= piso_permitido:
        return {"cerrar": True, "motivo": "trailing_tp", "pico_nuevo": pico_actual}
    if resultado_actual_pct <= 0:
        return {"cerrar": True, "motivo": "breakeven", "pico_nuevo": pico_actual}

    return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual}


SL_DIRECTIVAS_PCT = -4.5  # 14/09 — "JJ_Cripto_Bot_Directivas_Optimizacion.pdf", confirmado por Juanjo
PICO_ACTIVACION_DIRECTIVAS_PCT = 2.0
RETROCESO_DIRECTIVAS_PCT = 10  # fijo, no escalado — "elimina la ambición de capturar tendencias macro"


def evaluar_cierre_directivas(direccion: str, pico_maximo_pct: float, resultado_actual_pct: float) -> dict:
    """
    14/09 — Simulación de "JJ_Cripto_Bot_Directivas_Optimizacion.pdf":
    SL fijo -4,5% (más ajustado que fix28) y UN SOLO tramo de trailing
    (no 3 escalados por ATR) — activa en pico≥2%, retrocede un 10% fijo
    desde el pico. Pura, sin efectos en la base (para usar en
    simulación, no en posiciones reales).

    OJO — contradice nuestro propio backtest de 196 operaciones reales
    (13/09), que encontró -7,5% como el SL óptimo real, con los SL más
    ajustados (-4/-5/-6%) sistemáticamente peores por whipsaw. Por eso
    corre como simulación, NO como la estrategia real — para que los
    datos decidan en vez de asumir que el documento tiene razón.
    """
    if resultado_actual_pct <= SL_DIRECTIVAS_PCT:
        return {"cerrar": True, "motivo": "stop_loss", "pico_nuevo": pico_maximo_pct}

    pico_actual = max(pico_maximo_pct or 0, resultado_actual_pct)
    if pico_actual >= PICO_ACTIVACION_DIRECTIVAS_PCT:
        piso_permitido = pico_actual * (1 - RETROCESO_DIRECTIVAS_PCT / 100)
        if resultado_actual_pct <= piso_permitido:
            return {"cerrar": True, "motivo": "trailing_directivas", "pico_nuevo": pico_actual}

    return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual}


# ── 17/09 — Directiva V5.0: nueva PRINCIPAL, reemplaza a fix28 en el capital real ──
VOLUMEN_24H_MINIMO_USDT = 10_000_000
SPREAD_MAXIMO_PCT = 0.08

ADX_TECHO_V5_LARGO = 30
ADX_TECHO_V5_CORTO = 35
RSI_V5_LARGO_MAX = 45   # LARGO: RSI(15m) < 45 — "comprar el retroceso"
RSI_V5_CORTO_MIN = 55   # CORTO: RSI(15m) > 55 — "vender el agotamiento"

SL_V5_PCT = -4.5
PICO_ACTIVACION_V5_PCT = 2.2
RETROCESO_V5_PCT = 10  # fijo, un solo tramo


def evaluar_cierre_v5(direccion: str, pico_maximo_pct: float, resultado_actual_pct: float) -> dict:
    """
    17/09 — Directiva V5.0 (AHORA LA REAL, reemplaza a fix28): SL fijo
    -4,5%, trailing de un solo tramo (activa en pico≥2,2%, retrocede
    10% fijo). Misma estructura que evaluar_cierre_directivas (que
    sigue existiendo aparte, sin tocar, como comparación), con
    activación levemente distinta (2,2% vs 2,0%).

    MISMO RIESGO YA SEÑALADO EN DIRECTIVAS: un SL de -4,5% cae en la
    zona que nuestro propio backtest de 196 operaciones reales (13/09)
    mostró como PEOR que -7,5% (por whipsaw) — la diferencia esta vez
    es que V5.0 SÍ va a manejar capital real desde ahora, a pedido
    explícito de Juanjo.
    """
    if resultado_actual_pct <= SL_V5_PCT:
        return {"cerrar": True, "motivo": "stop_loss", "pico_nuevo": pico_maximo_pct}

    pico_actual = max(pico_maximo_pct or 0, resultado_actual_pct)
    if pico_actual >= PICO_ACTIVACION_V5_PCT:
        piso_permitido = pico_actual * (1 - RETROCESO_V5_PCT / 100)
        if resultado_actual_pct <= piso_permitido:
            return {"cerrar": True, "motivo": "trailing_v5", "pico_nuevo": pico_actual}

    return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual}


# ── 24/09 — Directiva V5.5 ("Estrategia Simplificada"): AHORA LA REAL,
# reemplaza a V5.0 en el capital de verdad (V5.0 pasa a modo sombra
# exclusivo desde acá). Objetivo declarado por Juanjo: resolver el
# "problema de asfixia por Stop Loss corto" que venía mostrando V5.0 en
# producción (-4,5% con trailing muy ajustado, cerrando en pérdida antes
# de que la posición pudiera desarrollarse).
#
# "OXÍGENO OPERATIVO (10x)": el -25,0% apalancado de acá corresponde a un
# leverage de 10x — que es exactamente LEVERAGE_FIJO ya vigente en este
# archivo (línea de arriba), así que no hace falta ningún cambio de
# apalancamiento real: -25,0%/10 = -2,5% de movimiento real del precio,
# tal cual lo pidió la directiva.
SL_V55_PCT = -25.0  # apalancado (10x) = -2.5% de movimiento real de precio
PICO_ACTIVACION_V55_PCT = 5.0  # apalancado (10x) = +0.5% de movimiento real
RETROCESO_V55_PCT = 10  # fijo, un solo tramo — igual patrón que V5.0/Directivas


def evaluar_cierre_v55(direccion: str, pico_maximo_pct: float, resultado_actual_pct: float) -> dict:
    """
    24/09 — Directiva V5.5 ("Estrategia Simplificada"): SL fijo -25,0%
    apalancado (10x => -2,5% real), trailing de un solo tramo (activa en
    pico≥5,0% apalancado => +0,5% real, retrocede 10% fijo desde el
    pico). Misma estructura que evaluar_cierre_v5/evaluar_cierre_directivas
    (SL + 1 tramo de trailing fijo), solo que con el SL mucho más ancho
    para evitar el "whipsaw" que venía mostrando V5.0 con su SL -4,5%.
    """
    if resultado_actual_pct <= SL_V55_PCT:
        return {"cerrar": True, "motivo": "stop_loss", "pico_nuevo": pico_maximo_pct}

    pico_actual = max(pico_maximo_pct or 0, resultado_actual_pct)
    if pico_actual >= PICO_ACTIVACION_V55_PCT:
        piso_permitido = pico_actual * (1 - RETROCESO_V55_PCT / 100)
        if resultado_actual_pct <= piso_permitido:
            return {"cerrar": True, "motivo": "trailing_v55", "pico_nuevo": pico_actual}

    return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual}


def evaluar_cierre_fix28_fiel(direccion: str, atr_pct: float, pico_maximo_pct: float, resultado_actual_pct: float,
                               precio_actual: float = None, rango_bajo: float = None, rango_alto: float = None,
                               fuera_rango_desde: str = None, btc_estado: str = None) -> dict:
    """
    16/09 — Réplica FIEL 1 a 1 de evaluar_cierre() (la función real de
    fix28), pero SIN escribir en la base — para la simulación "Real
    (fix28) fiel", a pedido de Juanjo: quiere ver en la comparación
    cómo rendiría el bot EXACTAMENTE como está configurado ahora
    (incluidas las 2 protecciones extra: fuera de rango y BTC en
    contra), sin arriesgar capital mientras sigue pausado.

    A diferencia de evaluar_cierre_simulado() (la "Simulación
    original", que a propósito NO tiene estas 2 protecciones), esta sí
    las incluye — es la réplica más fiel posible.

    Devuelve además "pico_nuevo" y "fuera_rango_desde_nuevo" para que
    el llamador los persista él mismo (acá no hay senal_id real).
    """
    if resultado_actual_pct <= SL_FIJO_PCT:
        return {"cerrar": True, "motivo": "stop_loss", "pico_nuevo": pico_maximo_pct, "fuera_rango_desde_nuevo": fuera_rango_desde}

    pico_actual = max(pico_maximo_pct or 0, resultado_actual_pct)
    nombre_tramo, retroceso_pct = calcular_tramo(pico_actual, atr_pct)
    umbral_breakeven, _, _ = _umbrales_por_atr(atr_pct)
    breakeven_activo = pico_actual >= umbral_breakeven

    if not breakeven_activo:
        return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": None}

    fuera_de_rango = False
    if precio_actual is not None and rango_bajo and rango_alto:
        fuera_de_rango = precio_actual < rango_bajo or precio_actual > rango_alto

    if fuera_de_rango and not fuera_rango_desde:
        fuera_rango_desde = datetime.now(TZ_ARG).isoformat()
    elif not fuera_de_rango and fuera_rango_desde:
        fuera_rango_desde = None

    if fuera_rango_desde:
        try:
            desde_dt = datetime.fromisoformat(fuera_rango_desde)
            horas_fuera = (datetime.now(TZ_ARG) - desde_dt).total_seconds() / 3600
            if horas_fuera >= HORAS_MAX_FUERA_DE_RANGO:
                return {"cerrar": True, "motivo": "fuera_rango_3hs", "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": fuera_rango_desde}
        except Exception:
            pass

    btc_en_contra = bool(btc_estado) and (
        (direccion == "LARGO" and btc_estado == "BAJISTA") or
        (direccion == "CORTO" and btc_estado == "ALCISTA")
    )
    modo_cauto = fuera_de_rango or btc_en_contra

    if retroceso_pct is None:
        if resultado_actual_pct <= 0:
            return {"cerrar": True, "motivo": "breakeven", "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": fuera_rango_desde}
        return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": fuera_rango_desde}

    retroceso_efectivo = retroceso_pct / 2 if modo_cauto else retroceso_pct
    piso_permitido = pico_actual * (1 - retroceso_efectivo)
    if resultado_actual_pct <= piso_permitido:
        motivo = "trailing_tp_cauto" if modo_cauto else "trailing_tp"
        return {"cerrar": True, "motivo": motivo, "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": fuera_rango_desde}
    if resultado_actual_pct <= 0:
        return {"cerrar": True, "motivo": "breakeven", "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": fuera_rango_desde}

    return {"cerrar": False, "motivo": None, "pico_nuevo": pico_actual, "fuera_rango_desde_nuevo": fuera_rango_desde}


def hay_lugar_para_abrir() -> dict:
    """Chequea el tope de 6 posiciones simultáneas y el tope de 2 aperturas por ciclo de 15min."""
    abiertas = db.contar_posiciones_abiertas()
    if abiertas >= MAX_POSICIONES_SIMULTANEAS:
        return {"hay_lugar": False, "motivo": f"tope de {MAX_POSICIONES_SIMULTANEAS} posiciones simultáneas"}

    aperturas_recientes = db.contar_aperturas_ultimos_minutos(15)
    if aperturas_recientes >= MAX_APERTURAS_POR_CICLO:
        return {"hay_lugar": False, "motivo": f"tope de {MAX_APERTURAS_POR_CICLO} aperturas cada 15 min"}

    return {"hay_lugar": True, "motivo": None}


def hay_lugar_para_abrir_v55() -> dict:
    """
    24/09 — Directiva V5.5: mismo chequeo que hay_lugar_para_abrir(), pero
    contra la tabla de simulación (simulaciones_v55) en vez de la real —
    para que la sombra de V5.5 refleje fielmente el mismo límite de 6
    posiciones simultáneas y 2 aperturas por ciclo de 15min que tendría
    si operara con capital real, incluso con el bot pausado (donde la
    tabla real no tiene ninguna posición contra la cual medir el tope).
    """
    abiertas = db.contar_simulaciones_v55_abiertas()
    if abiertas >= MAX_POSICIONES_SIMULTANEAS:
        return {"hay_lugar": False, "motivo": f"tope de {MAX_POSICIONES_SIMULTANEAS} posiciones simultáneas"}

    aperturas_recientes = db.contar_aperturas_v55_ultimos_minutos(15)
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
