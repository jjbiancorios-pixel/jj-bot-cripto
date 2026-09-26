"""
main.py — Bot Cripto (rediseño desde cero, 04/09/2026)
──────────────────────────────────────────────────────
Arquitectura: 1 proceso, dedicado SOLO a este cinturón (sin PAXG/BingX).
Diseño de referencia: JJ_Cripto_Bot_Rediseno_BotCripto.docx +
JJ_Cripto_Bot_Rediseno_BotCripto_EntradaV2.docx

ENTRADA — 3 gates + score (máx 10, umbral 7):
  Gate 1: ADX + DI confirma dirección — umbral 23 (BTC/ETH/majors) o 28 (resto)
  Gate 2: precio alineado con EMA20 de 4h
  Gate 3: funding rate no extremo (bloquea LARGO si muy positivo, CORTO si muy negativo)
  Score: familia momentum (RSI+StochRSI+MACD+Bollinger+vela) TOPEADA a 4pts
         + ATR + contexto BTC + confirmación 1h + volumen (1.5x mínimo)

GRILLA: rango = ATR%×3 con piso por ADX (6/7.5/9%), grillas = rango%/paso
        (paso nunca menor a 3x comisión ida+vuelta, ver gestion_riesgo)

RIESGO: SL fijo -4%, trailing TP por pico (ver gestion_riesgo.py) —
        chequeo DIRECTO a Pionex cada 2seg, en threading.Thread aparte
        (mismo patrón de v18: el escaneo de 15min NO puede bloquear esto)

CAPITAL: recálculo diario 00:01 ARG, 5% por posición, sin reserva.
Sin logging de detalle de gates desde el día 1 (recomendado por Claude,
ya que no hay ventana de sombra previa — se guarda en gates_log siempre,
califique o no, para poder diagnosticar rápido).
"""
import requests
import pandas as pd
import numpy as np
import time
import threading
import schedule
from datetime import datetime, timezone, timedelta
import os

import db
import telegram_cmds
import gestion_riesgo
import pionex_api

TZ_ARG = timezone(timedelta(hours=-3))

PARES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT",
    "LTCUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT", "XLMUSDT",
    "TRXUSDT", "AAVEUSDT", "ALGOUSDT", "ICPUSDT", "AXSUSDT",
    "SANDUSDT", "MANAUSDT", "GALAUSDT", "FTMUSDT", "NEARUSDT",
    "CHZUSDT", "CRVUSDT", "RUNEUSDT", "HBARUSDT",
    "ARBUSDT", "INJUSDT", "SUIUSDT", "WLDUSDT",
    "STXUSDT", "LDOUSDT", "SEIUSDT", "FETUSDT", "GRTUSDT",
    "WIFUSDT", "FLOKIUSDT",  # 10/09: sacado 1000PEPEUSDT — no disponible en Pionex Futures Grid
    "ENAUSDT", "TIAUSDT", "NOTUSDT", "TAOUSDT",
    "ORDIUSDT", "ACEUSDT", "ALTUSDT", "PORTALUSDT",
    "APTUSDT", "ARKMUSDT", "BLURUSDT", "GMTUSDT", "IMXUSDT",
    "JASMYUSDT", "JTOUSDT", "KASUSDT", "MASKUSDT",
    "ONDOUSDT", "PYTHUSDT", "ROSEUSDT", "SSVUSDT",
    "STRKUSDT", "SUPERUSDT", "TWTUSDT", "UMAUSDT", "WUSDT",
    "XAIUSDT", "ZETAUSDT", "ZRXUSDT",
    "TONUSDT", "EIGENUSDT", "MOVEUSDT", "VIRTUALUSDT",
    "PENGUUSDT", "MOCAUSDT", "SCRUSDT",
    # 17/09 — Directiva V5.0: ampliación de 77 a 120 pares (filtro de
    # volumen/spread hace la selección real en cada ciclo). AVISO
    # HONESTO: no pude confirmar 1 por 1 que cada uno esté disponible
    # específicamente en Pionex Futures Grid (mismo tipo de problema
    # que tuvimos con 1000PEPEUSDT) — evité a propósito otros tokens
    # con prefijo "1000x" por la misma razón, pero el resto conviene
    # vigilarlo los primeros días por si Pionex rechaza alguno con
    # "invalid symbol".
    "BCHUSDT", "FILUSDT", "APEUSDT", "EOSUSDT", "THETAUSDT",
    "KAVAUSDT", "ZILUSDT", "ENJUSDT", "1INCHUSDT", "COMPUSDT",
    "SNXUSDT", "YFIUSDT", "SUSHIUSDT", "BATUSDT", "ZECUSDT",
    "DASHUSDT", "QTUMUSDT", "ONTUSDT", "ICXUSDT", "KNCUSDT",
    "STORJUSDT", "CELOUSDT", "ANKRUSDT", "CTSIUSDT", "RSRUSDT",
    "OCEANUSDT", "BANDUSDT", "RLCUSDT", "COTIUSDT", "DYDXUSDT",
    "GMXUSDT", "WOOUSDT", "HOOKUSDT", "CFXUSDT", "MAGICUSDT",
    "HFTUSDT", "RDNTUSDT", "EDUUSDT", "IDUSDT", "CYBERUSDT",
    "ARUSDT", "ACHUSDT", "TRBUSDT",
]

# Pares "majors" — umbral de ADX más bajo (23 en vez de 28), porque
# sostienen tendencias más largas que los altcoins.
PARES_MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}

SCORE_MAX = 10
SCORE_UMBRAL = 8  # 06/09: subido de 7 a 8 — solo operar señales de mejor calidad
SCORE_MOMENTUM_TOPE = 4  # familia RSI+StochRSI+MACD+Bollinger+vela, topeada
VOLUMEN_RATIO_MINIMO = 1.5
FUNDING_UMBRAL_PCT = 0.05  # ±0.05%/8h — a calibrar con datos reales

HORA_INICIO, HORA_FIN = 7, 23  # 19/09: YA NO SE USA — bot debe operar 24hs, ver en_horario_operativo() más abajo


def hoy_arg():
    return datetime.now(TZ_ARG).strftime("%Y%m%d")


def en_horario_operativo() -> bool:
    """
    19/09 FIX: restricción de horario (7-23hs ARG) era un resabio de
    versiones muy tempranas — el bot debe operar las 24hs. Confirmado
    que esto era la causa real de que ciclo_seleccion se congelara
    todas las noches entre las 23hs y las 7hs, sin ningún error
    visible (era un `return` limpio, no una falla). Ahora siempre True.
    """
    return True


# ── Datos: cascada Bybit → OKX → Binance Vision (nunca Pionex) ─────
BYBIT_TF = {"15m": "15", "1h": "60", "4h": "240", "1d": "D"}
OKX_TF = {"15m": "15m", "1h": "1H", "4h": "4H", "1d": "1Dutc"}
BINANCE_TF = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1d"}


def OKX_PAR(p):
    return p.replace("1000PEPE", "PEPE").replace("USDT", "-USDT")


def _velas_bybit(par, tf, n):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={par}&interval={BYBIT_TF.get(tf,'15')}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError("bybit fail")
    rows = data["result"]["list"]
    if not rows or len(rows) < 20:
        raise ValueError("bybit empty")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "turnover"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)


def _velas_okx(par, tf, n):
    inst = OKX_PAR(par)
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={OKX_TF.get(tf,'15m')}&limit={n}"
    r = requests.get(url, timeout=8)
    rows = r.json().get("data", [])
    if not rows or len(rows) < 20:
        raise ValueError("okx empty")
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df.iloc[::-1].reset_index(drop=True)


def _velas_binance(par, tf, n):
    url = f"https://data-api.binance.vision/api/v3/klines?symbol={par}&interval={BINANCE_TF.get(tf,'15m')}&limit={n}"
    r = requests.get(url, timeout=8)
    data = r.json()
    if not isinstance(data, list) or len(data) < 20:
        raise ValueError("binance empty")
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "vol", "ct", "qav", "trades", "tbbav", "tbqav", "ignore"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    return df


def get_velas(par, tf, n=100):
    for f in (_velas_bybit, _velas_okx, _velas_binance):
        try:
            df = f(par, tf, n)
            if df is not None and len(df) >= 20:
                return df
        except Exception:
            continue
    return None


def _precio_bybit(par):
    r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={par}", timeout=6)
    data = r.json()
    if data.get("retCode") != 0:
        raise ValueError()
    return float(data["result"]["list"][0]["lastPrice"])


def _precio_okx(par):
    r = requests.get(f"https://www.okx.com/api/v5/market/ticker?instId={OKX_PAR(par)}", timeout=6)
    rows = r.json().get("data", [])
    if not rows:
        raise ValueError()
    return float(rows[0]["last"])


def _precio_binance(par):
    r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/price?symbol={par}", timeout=6)
    return float(r.json()["price"])


def get_precio(par):
    for f in (_precio_bybit, _precio_okx, _precio_binance):
        try:
            p = f(par)
            if p and p > 0:
                return p
        except Exception:
            continue
    return None


def get_funding_rate(par):
    """Funding rate actual (Bybit linear perpetuo) — % por intervalo de 8h."""
    try:
        r = requests.get(f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={par}", timeout=6)
        data = r.json()
        if data.get("retCode") == 0:
            fr = data["result"]["list"][0].get("fundingRate")
            if fr is not None:
                return float(fr) * 100  # a %
    except Exception:
        pass
    return None


# ── Indicadores ──────────────────────────────────────────────
def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return float((100 - 100 / (1 + g / l.replace(0, np.nan))).iloc[-1])


def calc_rsi_serie(s, p=14):
    """20/09 — Directiva V5.2 (Ranking de Fuerza): versión que devuelve la SERIE completa de RSI, no solo el último valor, para poder calcular la pendiente."""
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / l.replace(0, np.nan))


def calcular_pendiente_rsi_3_velas(serie_rsi_15m: list) -> float:
    """
    20/09 — Directiva V5.2, provista por Juanjo: pendiente del RSI por
    regresión lineal simple (mínimos cuadrados) sobre las últimas 3
    velas de 15m. Verificada con casos de control antes de integrar
    (RSI subiendo 5pts/vela -> pendiente 5.0; bajando 3pts/vela ->
    pendiente -3.0; plano -> 0.0 — los 3 dieron exacto).
    """
    if len(serie_rsi_15m) < 3:
        return 0.0
    y = serie_rsi_15m[-3:]
    x = [1, 2, 3]
    n = len(x)
    suma_x = sum(x)
    suma_y = sum(y)
    suma_xy = sum(i * j for i, j in zip(x, y))
    suma_x_cuadrado = sum(i ** 2 for i in x)
    denominador = (n * suma_x_cuadrado) - (suma_x ** 2)
    if denominador == 0:
        return 0.0
    pendiente_m = ((n * suma_xy) - (suma_x * suma_y)) / denominador
    return float(pendiente_m)


def aplicar_ranking_de_fuerza_v52(candidatos_calificados_ciclo: list) -> list:
    """
    20/09 — Directiva V5.2, provista por Juanjo. Clasifica los
    candidatos del ciclo de 15m según su aceleración de oscilador —
    solo los 2 mejores pasan a ejecutarse con capital real.

    FIX aplicado (confirmado por Juanjo): la fórmula de CORTO original
    usaba abs(pendiente) — igualaba un RSI subiendo rápido (mala señal
    para CORTO) con uno bajando rápido (buena señal). Corregido a
    pendiente CON signo, igual que LARGO — una pendiente positiva
    (RSI subiendo, en contra de un CORTO) ahora resta score en vez de
    sumar igual que una caída.
    """
    if not candidatos_calificados_ciclo:
        return []

    for par_info in candidatos_calificados_ciclo:
        rsi_15m = par_info.get("rsi_15m")
        pendiente = par_info.get("pendiente_rsi", 0.0)

        if rsi_15m is None:
            par_info["fuerza_score"] = -999.0
            continue

        if par_info["direccion"] == "LARGO":
            par_info["fuerza_score"] = (45.0 - rsi_15m) * pendiente
        else:
            # CORREGIDO 20/09: pendiente CON signo (negativa = RSI
            # cayendo = buena señal para CORTO), no abs(pendiente)
            par_info["fuerza_score"] = (rsi_15m - 55.0) * -pendiente

    candidatos_ordenados = sorted(candidatos_calificados_ciclo, key=lambda x: x.get("fuerza_score", -999.0), reverse=True)
    return candidatos_ordenados[:2]


def aplicar_ranking_v55(candidatos_calificados_ciclo: list) -> list:
    """
    24/09 — Directiva V5.5 ("Estrategia Simplificada"), provista por
    Juanjo. Reemplaza a V5.0 (aplicar_ranking_de_fuerza_v52) como la
    que opera con capital REAL — V5.0 pasa a modo sombra exclusivo.

    Punto 1 ("ELIMINACIÓN DE PENDIENTES"): a diferencia de V5.2, este
    ranking NO usa la pendiente de RSI para nada — se deja el cálculo
    de pendiente intacto en el resto del código porque "V5.0 fiel"
    (que sigue vigente en sombra, según lo pedido explícitamente por
    Juanjo: "siguen todas las estrategias vigentes y en modo sombra")
    todavía depende de él para su propio ranking. Acá simplemente no
    se lee ese campo.

    Punto 2 ("RANKING DE FUERZA PURO"): score por resta directa contra
    el umbral de RSI de entrada de V5.0 (45 LARGO / 55 CORTO) — cuanto
    más lejos esté el RSI(15m) de ese umbral, en la dirección
    correcta, más alto el score. Se ordena de mayor a menor y se
    toman los 2 mejores del ciclo de 15 minutos — mismo patrón de
    selección que V5.2, ranking distinto.
    """
    if not candidatos_calificados_ciclo:
        return []

    for par_info in candidatos_calificados_ciclo:
        rsi_15m = par_info.get("rsi_15m")

        if rsi_15m is None:
            par_info["fuerza_score_v55"] = -999.0
            continue

        if par_info["direccion"] == "LARGO":
            par_info["fuerza_score_v55"] = 45.0 - rsi_15m
        else:
            par_info["fuerza_score_v55"] = rsi_15m - 55.0

    candidatos_ordenados = sorted(candidatos_calificados_ciclo, key=lambda x: x.get("fuerza_score_v55", -999.0), reverse=True)

    # 26/09 — Directiva: capturar el LOTE COMPLETO del ciclo (ejecutados
    # y descartados) justo ACÁ, antes del recorte a los 2 mejores, y
    # persistirlo en SQLite de forma masiva. Esto es lo único que
    # permite ver después cantidad y calidad de las señales que
    # calificaron pero no llegaron a simularse.
    try:
        db.guardar_candidatos_v55_ciclo(candidatos_ordenados)
    except Exception as e:
        print(f"Error guardando candidatos_v55_ciclo: {e}")

    # 26/09 — Directiva (v2): abre sombra continua (no capital real, sin
    # tope) para los primeros TOP_SOMBRA_RANKED_V55 del ranking — se
    # evalúan con la MISMA lógica de salida real (evaluar_cierre_v55) en
    # el hilo de 2seg, para poder backtestear después CUALQUIER
    # combinación de aperturas/tope con resultados reales, no una
    # aproximación por precio a horas fijas.
    try:
        db.abrir_sombra_ranked_v55_lote(candidatos_ordenados)
    except Exception as e:
        print(f"Error abriendo sombra_ranked_v55: {e}")

    return candidatos_ordenados[:2]


def calc_atr(df, p=14):
    hl = df["high"] - df["low"]
    hcp = (df["high"] - df["close"].shift()).abs()
    lcp = (df["low"] - df["close"].shift()).abs()
    return float(pd.concat([hl, hcp, lcp], axis=1).max(axis=1).rolling(p).mean().iloc[-1])


def calc_bb(s, p=20):
    m = s.rolling(p).mean()
    st = s.rolling(p).std()
    up = (m + 2 * st).iloc[-1]
    dn = (m - 2 * st).iloc[-1]
    mid = m.iloc[-1]
    ancho = (up - dn) / mid * 100 if mid > 0 else 0
    pos = (s.iloc[-1] - dn) / (up - dn) if (up - dn) > 0 else 0.5
    return {"upper": up, "lower": dn, "mid": mid, "ancho": ancho, "pos": pos}


def calc_macd(s):
    m = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    sg = m.ewm(span=9).mean()
    return {
        "macd": float(m.iloc[-1]), "signal": float(sg.iloc[-1]), "hist": float((m - sg).iloc[-1]),
        "cruce_alc": bool(m.iloc[-1] > sg.iloc[-1] and m.iloc[-2] <= sg.iloc[-2]),
        "cruce_baj": bool(m.iloc[-1] < sg.iloc[-1] and m.iloc[-2] >= sg.iloc[-2]),
    }


def calc_ema(s, p):
    return float(s.ewm(span=p).mean().iloc[-1])


def calc_stoch_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    rsi = 100 - 100 / (1 + g / l.replace(0, np.nan))
    mn = rsi.rolling(p).min()
    mx = rsi.rolling(p).max()
    return float(((rsi - mn) / (mx - mn + 1e-10) * 100).iloc[-1])


def calc_adx(df, p=14):
    """ADX + DI+/DI- — método de Wilder. Reutilizado de v18 (ya validado)."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    atr_w = tr.ewm(alpha=1 / p, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / p, adjust=False).mean() / atr_w.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / p, adjust=False).mean() / atr_w.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(alpha=1 / p, adjust=False).mean()
    # 08/09: ADX de 3 velas atrás, para detectar si la tendencia ya viene
    # perdiendo fuerza (ADX cayendo) aunque el valor actual siga alto —
    # entrar con ADX extremo y en baja suele ser entrar tarde, cerca del
    # agotamiento de la tendencia (3 pérdidas reales: ADX 39/40/49).
    adx_hace_3 = float(adx.iloc[-4]) if len(adx) >= 4 else float(adx.iloc[-1])
    return {"adx": float(adx.iloc[-1]), "plus_di": float(plus_di.iloc[-1]), "minus_di": float(minus_di.iloc[-1]), "adx_hace_3": adx_hace_3}


def patron_vela_score(df, direccion: str) -> int:
    """
    Patrón de vela (0, 1 o 2 puntos) — parte de la familia momentum topeada.

    04/09 FIX: antes sumaba puntos por cualquier vela con cuerpo fuerte,
    SIN chequear si esa vela iba en la misma dirección de la señal — una
    vela bajista fuerte podía sumarle puntos a una candidata LARGO. Ahora
    exige que la vela confirme la dirección (alcista para LARGO, bajista
    para CORTO) antes de puntuar.
    """
    c, o = df["close"].iloc[-1], df["open"].iloc[-1]
    h, l = df["high"].iloc[-1], df["low"].iloc[-1]
    vela_alcista = c > o
    if (direccion == "LARGO" and not vela_alcista) or (direccion == "CORTO" and vela_alcista):
        return 0
    cuerpo = abs(c - o)
    rango_total = h - l
    if rango_total <= 0:
        return 0
    if cuerpo / rango_total > 0.6:
        return 2  # vela con cuerpo fuerte, dirección clara y a favor
    if cuerpo / rango_total > 0.3:
        return 1
    return 0


def calc_grillas(rango_pct: float) -> int:
    """
    Cantidad de grillas: rango% / paso objetivo, con piso 15 / techo 200.
    El paso nunca puede ser menor a 3x la comisión ida+vuelta (0.10% × 3 =
    0.30%) — si no, se ensanchan los escalones para no perder plata en
    comisiones. No existe API de Pionex que recomiende esto (confirmado).
    """
    paso_objetivo = max(0.20, pionex_api.COMISION_IDA_VUELTA_PCT * 3)
    grillas = int(rango_pct / paso_objetivo)
    return max(15, min(200, grillas))


def calcular_grid(precio: float, atr_pct: float, adx: float) -> dict:
    """Rango = ATR%×3, piso mínimo por ADX (6/7.5/9%)."""
    if adx < 25:
        piso = 6.0
    elif adx <= 35:
        piso = 7.5
    else:
        piso = 9.0
    rango_pct = max(atr_pct * 3, piso)
    grillas = calcular_grillas_wrapper(rango_pct)
    top = precio * (1 + rango_pct / 200)
    bottom = precio * (1 - rango_pct / 200)
    return {"rango_pct": round(rango_pct, 2), "top": top, "bottom": bottom, "grillas": grillas}


def calcular_grillas_wrapper(rango_pct):
    return calc_grillas(rango_pct)


# ── BTC — contexto general ──────────────────────────────────
def analizar_btc():
    """
    10/09 — Se agregó detección de "cambio reciente" de tendencia (mismo
    principio que la persistencia de 3 velas ya usada para cada moneda,
    aplicado acá al contexto de BTC) — sirve para activar el modo cauto
    de exposición direccional (ver actualizar_modo_cauto_btc).
    """
    df = get_velas("BTCUSDT", "1h", 100)
    if df is None:
        return {"estado": "SIN_DATO", "cambio_1h_pct": 0, "cambio_reciente": False, "persistio_3": False}
    ema9_serie = df["close"].ewm(span=9).mean()
    ema21_serie = df["close"].ewm(span=21).mean()
    ema9 = float(ema9_serie.iloc[-1])
    ema21 = float(ema21_serie.iloc[-1])
    cambio_1h_pct = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2] * 100

    diff_pct = abs(ema9 - ema21) / ema21 * 100 if ema21 > 0 else 0
    if diff_pct < 0.05:
        estado = "LATERAL"
    elif ema9 > ema21:
        estado = "ALCISTA"
    else:
        estado = "BAJISTA"

    diff_serie = (ema9_serie - ema21_serie)
    signo_ahora = 1 if ema9 > ema21 else -1
    signo_hace_3 = 1 if diff_serie.iloc[-4] > 0 else -1 if len(diff_serie) >= 4 else signo_ahora
    cambio_reciente = estado != "LATERAL" and signo_ahora != signo_hace_3
    persistio_3 = bool((np.sign(diff_serie.iloc[-3:]) == signo_ahora).all())

    return {"estado": estado, "cambio_1h_pct": round(cambio_1h_pct, 3),
            "cambio_reciente": cambio_reciente, "persistio_3": persistio_3}


def actualizar_modo_cauto_btc(btc: dict):
    """
    10/09 — Activa el modo cauto (límite de 3-de-6 posiciones por
    dirección) cuando BTC muestra un cambio reciente de tendencia. Se
    desactiva solo, automático, cuando pasa cualquiera de estas 2 cosas:
    - la tendencia nueva se confirma (3 velas seguidas sostenidas, misma
      técnica ya usada para cada moneda individual)
    - BTC vuelve a lateral (sin riesgo real de lado equivocado)
    """
    estado_cauto = db.obtener_estado_btc_cauto()
    if not estado_cauto["activo"]:
        if btc.get("cambio_reciente"):
            db.guardar_estado_btc_cauto(True)
    else:
        if btc["estado"] == "LATERAL" or btc.get("persistio_3"):
            db.guardar_estado_btc_cauto(False)


# ── Análisis de un par: 3 gates + score ─────────────────────
def pasa_filtro_universo(par: str) -> bool:
    """
    17/09 — Directiva V5.0: filtro previo, ANTES de evaluar cualquier
    gate. Descarta si volumen 24h < 10M USDT o spread > 0,08%. Sin
    dato confiable de alguno de los 2, descarta por seguridad (mejor
    perderse una señal que operar en algo demasiado ilíquido).
    """
    try:
        r = requests.get(f"https://data-api.binance.vision/api/v3/ticker/24hr?symbol={par}", timeout=6)
        data = r.json()
        volumen_24h_usdt = float(data.get("quoteVolume", 0))
        if volumen_24h_usdt < gestion_riesgo.VOLUMEN_24H_MINIMO_USDT:
            return False
    except Exception:
        return False

    try:
        r2 = requests.get(f"https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol={par}", timeout=6)
        data2 = r2.json()
        bid = float(data2.get("bidPrice", 0))
        ask = float(data2.get("askPrice", 0))
        if bid <= 0:
            return False
        spread_pct = (ask - bid) / bid * 100
        if spread_pct > gestion_riesgo.SPREAD_MAXIMO_PCT:
            return False
    except Exception:
        return False

    return True


def analizar_par_v5(par: str, btc: dict):
    """
    17/09 — Directiva V5.0 (AHORA LA PRINCIPAL): reemplaza por completo
    el score/momentum antiguo. Gates que se MANTIENEN: EMA20 4h,
    persistencia (3 velas, 1h), funding rate. Gate NUEVO que reemplaza
    ADX+DI+score: ADX(1h)≤30 y RSI(15m)<45 para LARGO; ADX(1h)≤35 y
    RSI(15m)>55 para CORTO — sin piso mínimo de ADX (tal cual lo pidió
    Juanjo, a diferencia del diseño anterior que sí tenía piso).
    """
    if not pasa_filtro_universo(par):
        db.guardar_gates_log(par, "FILTRO_UNIVERSO", 0, 0, False, False, False, 0, 0, False, None, None, None, estrategia="v5")
        return None

    df15 = get_velas(par, "15m", 100)
    df1h = get_velas(par, "1h", 100)
    df4h = get_velas(par, "4h", 100)
    if df15 is None or df1h is None or df4h is None:
        db.guardar_gates_log(par, "SIN_DATOS", 0, 0, False, False, False, 0, 0, False, None, None, None, estrategia="v5")
        return None

    precio = df15["close"].iloc[-1]

    ema9_1h = calc_ema(df1h["close"], 9)
    ema21_1h = calc_ema(df1h["close"], 21)
    diferencia_ema_pct = abs(ema9_1h - ema21_1h) / ema21_1h * 100 if ema21_1h > 0 else 0
    if diferencia_ema_pct < 0.05:
        db.guardar_gates_log(par, "SIN_DIRECCION_CLARA", 0, 0, False, False, False, 0, 0, False, None, None, None, estrategia="v5")
        return None
    direccion = "LARGO" if ema9_1h > ema21_1h else "CORTO"

    # Persistencia (se mantiene, sin cambios respecto al diseño anterior)
    ema9_serie = df1h["close"].ewm(span=9).mean()
    ema21_serie = df1h["close"].ewm(span=21).mean()
    diff_serie = (ema9_serie - ema21_serie).iloc[-3:]
    signo_actual = 1 if direccion == "LARGO" else -1
    persistio = bool((np.sign(diff_serie) == signo_actual).all())
    if not persistio:
        db.guardar_gates_log(par, "SIN_PERSISTENCIA", 0, 0, False, False, False, 0, 0, False, None, None, None, estrategia="v5")
        return None

    # EMA20 4h (se mantiene)
    ema20_4h = calc_ema(df4h["close"], 20)
    paso_ema4h = (precio > ema20_4h) if direccion == "LARGO" else (precio < ema20_4h)
    if not paso_ema4h:
        db.guardar_gates_log(par, direccion, 0, 0, True, False, False, 0, 0, False, None, None, None, estrategia="v5")
        return None

    # Funding rate (se mantiene)
    funding = get_funding_rate(par)
    paso_funding = True
    if funding is not None:
        if direccion == "LARGO" and funding > 0.05:
            paso_funding = False
        elif direccion == "CORTO" and funding < -0.05:
            paso_funding = False
    if not paso_funding:
        db.guardar_gates_log(par, direccion, 0, 0, True, True, False, 0, 0, False, None, None, None, estrategia="v5")
        return None

    # ── Gate NUEVO de V5.0: ADX(1h) + RSI(15m), reemplaza ADX+DI+score ──
    adx_info = calc_adx(df1h)
    adx = adx_info["adx"]
    rsi_serie = calc_rsi_serie(df15["close"])
    rsi_15m = float(rsi_serie.iloc[-1])
    pendiente_rsi = calcular_pendiente_rsi_3_velas(rsi_serie.tolist())
    atr_abs = calc_atr(df15)
    atr_pct = atr_abs / precio * 100 if precio > 0 else 0

    if direccion == "LARGO":
        paso_v5 = adx <= gestion_riesgo.ADX_TECHO_V5_LARGO and rsi_15m < gestion_riesgo.RSI_V5_LARGO_MAX
    else:
        paso_v5 = adx <= gestion_riesgo.ADX_TECHO_V5_CORTO and rsi_15m > gestion_riesgo.RSI_V5_CORTO_MIN

    db.guardar_gates_log(par, direccion, adx, 0, True, True, True, 10 if paso_v5 else 0, 0, paso_v5, atr_pct, rsi_15m, None, estrategia="v5")

    if not paso_v5:
        return None

    grid = calcular_grid(precio, atr_pct, adx)

    return {
        "par": par, "direccion": direccion, "precio": precio,
        "adx": round(adx, 2), "rsi": round(rsi_15m, 2), "atr_pct": round(atr_pct, 3),
        "rsi_15m": rsi_15m, "pendiente_rsi": pendiente_rsi,  # 20/09 — Directiva V5.2 (Ranking de Fuerza)
        "score": 10, "razones": [f"V5.0: ADX(1h)={round(adx,1)} RSI(15m)={round(rsi_15m,1)}"],
        "rango_pct": grid["rango_pct"], "rango_bajo": round(grid["bottom"], 6),
        "rango_alto": round(grid["top"], 6), "grillas": grid["grillas"],
    }


def analizar_par(par: str, btc: dict):
    df15 = get_velas(par, "15m", 100)
    df1h = get_velas(par, "1h", 100)
    df4h = get_velas(par, "4h", 100)
    if df15 is None or df1h is None or df4h is None:
        return None

    precio = df15["close"].iloc[-1]
    atr_abs = calc_atr(df15)
    atr_pct = atr_abs / precio * 100

    adx_info = calc_adx(df1h)
    adx = adx_info["adx"]
    plus_di, minus_di = adx_info["plus_di"], adx_info["minus_di"]
    adx_hace_3 = adx_info["adx_hace_3"]

    ema20_4h = calc_ema(df4h["close"], 20)
    ema9_1h = calc_ema(df1h["close"], 9)
    ema21_1h = calc_ema(df1h["close"], 21)

    # Dirección candidata (04/09, FIX de robustez): antes comparaba precio
    # vs. EMA9 de 15min (una sola vela, muy sensible al ruido). Ahora usa
    # el cruce EMA9/EMA21 en 1h — menos ruidoso, timeframe más alto, y una
    # cruz de 2 medias en vez de precio vs. 1 sola media. Si no hay
    # dirección clara (EMAs casi pegadas), se descarta el candidato acá
    # mismo, antes de gastar cómputo en los gates.
    diferencia_ema_pct = abs(ema9_1h - ema21_1h) / ema21_1h * 100 if ema21_1h > 0 else 0
    if diferencia_ema_pct < 0.05:  # EMAs casi pegadas, sin tendencia clara en 1h
        return None
    direccion = "LARGO" if ema9_1h > ema21_1h else "CORTO"

    # 07/09 — PERSISTENCIA DE TENDENCIA (calculada en UNA sola pasada,
    # no repitiendo el análisis 2 veces): exige que la dirección
    # (EMA9 vs EMA21 en 1h) se haya sostenido en las últimas 3 velas, no
    # solo en la actual — descarta cruces de un solo instante que
    # revierten enseguida, sin la trampa histórica de v18 (exigir que la
    # combinación COMPLETA de filtros se repita en 2 evaluaciones
    # SEPARADAS resultó en 0% de señales sobreviviendo, por multiplicar
    # probabilidades ya bajas en vez de sumarlas). Acá se mira el
    # historial ya disponible en df1h, sin ningún costo extra de cómputo
    # ni de consultas.
    N_VELAS_PERSISTENCIA = 3
    ema9_serie = df1h["close"].ewm(span=9).mean()
    ema21_serie = df1h["close"].ewm(span=21).mean()
    diff_serie = (ema9_serie - ema21_serie).iloc[-N_VELAS_PERSISTENCIA:]
    signo_actual = 1 if direccion == "LARGO" else -1
    persistio = bool((np.sign(diff_serie) == signo_actual).all())
    if not persistio:
        db.guardar_gates_log(par, "SIN_PERSISTENCIA", adx, 0, False, False, False, 0, 0, False, atr_pct, None, None)
        return None

    # ── GATE 1: ADX + DI (umbral diferenciado por tipo de par) ──
    # 11/09 — REVERTIDO a los parámetros validados por backtest real
    # sobre 196 operaciones (bot_cripto_backup_20260911): score>=8 +
    # ADX techo 37 dio +56,31% neto / 76,1% win rate (n=88), la mejor
    # combinación robusta encontrada. El techo se había subido a 47 y
    # abandonado a 37 antes por impaciencia (0 señales una noche), sin
    # evidencia real de que estuviera mal — el backtest confirma que 37
    # era la decisión correcta. También coincide con doctrina externa
    # (ADX>40-45 = "blow-off top"/fase de agotamiento, documentado en
    # múltiples fuentes independientes).
    #
    # 13/09 — SEGUNDO hallazgo, con 204 operaciones: LARGO rinde bastante
    # peor que CORTO (60,0% vs 72,5% win rate), y el ADX promedio de las
    # LARGO es sistemáticamente más alto que el de las CORTO (≈40 vs
    # ≈35) — entran más tarde en el movimiento. Backtest específico:
    # bajar el techo de LARGO a 33 reduce la pérdida neta de -72,73% a
    # -7,74% (mejora fuerte, aunque no la vuelve positiva del todo — hay
    # algo más que el ADX no termina de explicar). Techo de CORTO se
    # mantiene en 37 (ya validado). Techos ahora ASIMÉTRICOS por dirección.
    adx_umbral = 23 if par in PARES_MAJORS else 28
    ADX_TECHO_LARGO = 33
    ADX_TECHO_CORTO = 37
    ADX_TECHO = ADX_TECHO_LARGO if direccion == "LARGO" else ADX_TECHO_CORTO
    di_confirma = (plus_di > minus_di) if direccion == "LARGO" else (minus_di > plus_di)
    paso_adx = adx > adx_umbral and adx <= ADX_TECHO and di_confirma
    if not paso_adx:
        db.guardar_gates_log(par, direccion, adx, adx_umbral, False, False, False, 0, 0, False, atr_pct, None, None)
        return None

    # 11/09 — Sacado el filtro de sobreextensión (vela vs ATR + RSI):
    # sin evidencia propia (no se pudo backtestear, esos datos no se
    # guardaban para candidatos rechazados) y coincide con el cambio
    # más reciente antes del peor resultado del día (-27,36% neto,
    # 53,8% win rate) — sospecha razonable sin poder confirmarla del
    # todo. El filtro de régimen BTC (probado y descartado por
    # backtest: -70,23% neto, peor que no usarlo) tampoco se agrega.
    rsi_actual = calc_rsi(df15["close"])  # se sigue calculando: ahora se GUARDA en gates_log para el próximo backtest

    # ── GATE 2: alineación EMA20 4h ──
    paso_ema4h = (precio > ema20_4h) if direccion == "LARGO" else (precio < ema20_4h)
    if not paso_ema4h:
        db.guardar_gates_log(par, direccion, adx, adx_umbral, True, False, False, 0, 0, False, atr_pct, rsi_actual, None)
        return None

    # ── GATE 3: funding rate no extremo ──
    funding = get_funding_rate(par)
    paso_funding = True
    if funding is not None:
        if direccion == "LARGO" and funding > FUNDING_UMBRAL_PCT:
            paso_funding = False
        elif direccion == "CORTO" and funding < -FUNDING_UMBRAL_PCT:
            paso_funding = False
    if not paso_funding:
        db.guardar_gates_log(par, direccion, adx, adx_umbral, True, True, False, 0, 0, False, atr_pct, rsi_actual, None)
        return None

    # ── SCORE (máx 10, umbral 7) — solo llegan acá los que ya pasaron los 3 gates ──
    razones = []
    score_momentum_bruto = 0  # antes de topear

    rsi = calc_rsi(df15["close"])
    if direccion == "LARGO" and rsi < 40:
        score_momentum_bruto += 2; razones.append(f"RSI favorable {rsi:.0f}")
    elif direccion == "CORTO" and rsi > 60:
        score_momentum_bruto += 2; razones.append(f"RSI favorable {rsi:.0f}")

    stoch = calc_stoch_rsi(df15["close"])
    if direccion == "LARGO" and stoch < 30:
        score_momentum_bruto += 1; razones.append("StochRSI sobrevendido")
    elif direccion == "CORTO" and stoch > 70:
        score_momentum_bruto += 1; razones.append("StochRSI sobrecomprado")

    macd = calc_macd(df15["close"])
    if (direccion == "LARGO" and macd["cruce_alc"]) or (direccion == "CORTO" and macd["cruce_baj"]):
        score_momentum_bruto += 2; razones.append("Cruce MACD a favor")

    bb = calc_bb(df15["close"])
    if direccion == "LARGO" and bb["pos"] < 0.3:
        score_momentum_bruto += 1; razones.append("Cerca de banda inferior Bollinger")
    elif direccion == "CORTO" and bb["pos"] > 0.7:
        score_momentum_bruto += 1; razones.append("Cerca de banda superior Bollinger")

    score_momentum_bruto += patron_vela_score(df15, direccion)

    score_momentum = min(SCORE_MOMENTUM_TOPE, score_momentum_bruto)

    score_independiente = 0
    # ATR (volatilidad suficiente para que valga la pena el grid)
    if atr_pct >= 0.5:
        score_independiente += 2; razones.append(f"ATR {atr_pct:.2f}% (volatilidad ok)")

    # Contexto BTC
    if (direccion == "LARGO" and btc["estado"] == "ALCISTA") or (direccion == "CORTO" and btc["estado"] == "BAJISTA"):
        score_independiente += 2; razones.append(f"BTC alineado ({btc['estado']})")
    elif btc["estado"] == "LATERAL":
        score_independiente += 1; razones.append("BTC lateral (neutro)")

    # Confirmación 1h — 04/09: cambiado a EMA21 (no EMA9) porque EMA9(1h)
    # ya se usa para determinar la dirección candidata más arriba; usar la
    # misma EMA de nuevo acá sería redundante (mismo dato contado 2 veces).
    if (direccion == "LARGO" and precio > ema21_1h) or (direccion == "CORTO" and precio < ema21_1h):
        score_independiente += 1; razones.append("Confirma en 1h (por encima/debajo de EMA21)")

    # Volumen (umbral 1.5x)
    vol_prom = df15["vol"].iloc[-21:-1].mean()
    vol_actual = df15["vol"].iloc[-1]
    volumen_ratio = vol_actual / vol_prom if vol_prom > 0 else None
    if volumen_ratio is not None and volumen_ratio >= VOLUMEN_RATIO_MINIMO:
        score_independiente += 1; razones.append(f"Volumen {volumen_ratio:.1f}x")

    score_total = score_momentum + score_independiente
    califico = score_total >= SCORE_UMBRAL

    db.guardar_gates_log(par, direccion, adx, adx_umbral, True, True, True,
                          score_total, score_momentum, califico, atr_pct, rsi_actual, volumen_ratio)

    if not califico:
        return None

    grid = calcular_grid(precio, atr_pct, adx)

    return {
        "par": par, "direccion": direccion, "precio": precio,
        "adx": round(adx, 2), "adx_umbral_usado": adx_umbral, "di_confirma": di_confirma,
        "ema4h_alineada": paso_ema4h, "funding_rate": funding, "funding_bloqueo": False,
        "score": score_total, "score_momentum": score_momentum, "razones": razones,
        "atr_pct": round(atr_pct, 3), "rsi": round(rsi, 2), "stoch_rsi": round(stoch, 2),
        "rango_pct": grid["rango_pct"], "rango_bajo": round(grid["bottom"], 6),
        "rango_alto": round(grid["top"], 6), "grillas": grid["grillas"],
    }


# ── Apertura real ────────────────────────────────────────────
def abrir_posicion_real(candidato: dict):
    capital = gestion_riesgo.calcular_capital_por_operacion()
    if capital is None:
        print("⚠️ Capital del día todavía no disponible — no se abre nada este ciclo.")
        return

    senal_id = db.guardar_senal(candidato)

    trend = "long" if candidato["direccion"] == "LARGO" else "short"
    resultado = pionex_api.crear_grilla_futuros_segura(
        par=candidato["par"].replace("USDT", ""),
        top=candidato["rango_alto"], bottom=candidato["rango_bajo"],
        row=candidato["grillas"], capital_objetivo_usdt=capital,
        leverage=gestion_riesgo.LEVERAGE_FIJO, trend=trend,
        sl_pct=gestion_riesgo.SL_FIJO_PCT,  # 04/09: SL nativo de respaldo, además del monitoreo activo
    )

    if not resultado["ok"]:
        telegram_cmds.enviar(
            f"⚠️ Falló la apertura de {candidato['par']} {candidato['direccion']}\n"
            f"Capital intentado: USD {resultado['capital_usado']:.2f} ({resultado['intentos']} intento/s)\n"
            f"<code>{str(resultado['resultado'])[:300]}</code>"
        )
        db.cerrar_senal(senal_id, 0, "apertura_fallida")
        return

    bu_order_id = resultado["resultado"].get("data", {}).get("buOrderId") or resultado["resultado"].get("data", {}).get("orderId")
    if not bu_order_id:
        telegram_cmds.enviar(f"⚠️ {candidato['par']}: Pionex respondió OK pero sin bu_order_id — REVISAR manualmente.\n<code>{str(resultado['resultado'])[:300]}</code>")
        db.cerrar_senal(senal_id, 0, "apertura_sin_id")
        return

    db.guardar_bu_order_id(senal_id, bu_order_id, resultado["capital_usado"], gestion_riesgo.LEVERAGE_FIJO)
    telegram_cmds.enviar(
        f"✅ <b>{candidato['par']} {candidato['direccion']}</b>\n"
        f"Score: {candidato['score']}/{SCORE_MAX} | ADX: {candidato['adx']}\n"
        f"Capital: USD {resultado['capital_usado']:.2f} | Rango: {candidato['rango_pct']}% | Grillas: {candidato['grillas']}\n"
        f"{' | '.join(candidato['razones'][:4])}"
    )


# ── Ciclo de selección (cada 15 min) ────────────────────────
def ciclo_seleccion():
    """
    05/09 FIX (mismo bug encontrado en PAXG): antes, estar en pausa
    cortaba TODO el ciclo antes de analizar — no se registraba nada en
    gates_log mientras el bot estaba pausado, así que pausar para
    observar sin arriesgar capital no servía para juntar evidencia.
    Ahora el análisis y el registro de CADA par SIEMPRE corren; la
    pausa (y el tope de posiciones) solo bloquean el paso final de
    abrir una posición real.
    """
    if not en_horario_operativo():
        return

    pausado = db.esta_pausado_global()

    btc = analizar_btc()
    actualizar_modo_cauto_btc(btc)
    modo_cauto_activo = db.obtener_estado_btc_cauto()["activo"]

    # 20/09 — Directiva V5.2 (Ranking de Fuerza): ya NO se abre apenas
    # un candidato de V5.0 califica — se juntan TODOS los candidatos
    # calificados del ciclo (120 pares), se rankean por fuerza de
    # oscilador, y solo los 2 mejores pasan a abrir con capital real
    # (y a alimentar "V5.0 fiel", para que la comparación siga siendo
    # fiel a lo que realmente hace la real). Objetivo: eliminar la
    # asignación de capital "por orden de aparición" en la lista de
    # pares, que no tiene ningún fundamento de calidad de señal.
    candidatos_v5_calificados = []

    for par in PARES:
        if db.par_tiene_posicion_abierta(par):
            continue

        try:
            candidato_v5 = analizar_par_v5(par, btc)
        except Exception as e:
            print(f"Error analizando {par} (V5.0): {e}")
            candidato_v5 = None

        if candidato_v5:
            candidatos_v5_calificados.append(candidato_v5)

        try:
            candidato = analizar_par(par, btc)
        except Exception as e:
            print(f"Error analizando {par}: {e}")
            continue
        if not candidato:
            continue

        # 13/09 — Simulación paralela (sin capital real), SIEMPRE que
        # no haya ya una abierta para este par — corre pase lo que pase
        # con la pausa, para tener datos de comparación constantes.
        if not db.par_tiene_simulacion_abierta(par):
            db.crear_simulacion(par, candidato["direccion"], candidato.get("score"),
                                 candidato.get("adx"), candidato.get("atr_pct"), candidato["precio"])

        # 14/09 — Simulación de "Directivas de Optimización": mismo
        # candidato (ya pasó los gates reales), pero con un filtro EXTRA
        # más estricto para LARGO — techo de ADX 30 (no 33) y bloqueo
        # por osciladores en sobrecompra macro (RSI>70 o StochRSI>80).
        # CORTO no cambia (el documento no lo menciona).
        calif_directivas = True
        if candidato["direccion"] == "LARGO":
            if candidato.get("adx", 0) > 30:
                calif_directivas = False
            if (candidato.get("rsi") or 0) > 70 or (candidato.get("stoch_rsi") or 0) > 80:
                calif_directivas = False
        if calif_directivas and not db.par_tiene_simulacion_directivas_abierta(par):
            db.crear_simulacion_directivas(par, candidato["direccion"], candidato.get("score"),
                                            candidato.get("adx"), candidato.get("atr_pct"), candidato["precio"])

        # 16/09 — 4ta simulación ("combo"): misma entrada de Directivas
        # (calif_directivas ya calculado arriba) + la SALIDA de la
        # simulación original (SL -7.5% + trailing 3 tramos por ATR,
        # ya validada con backtest propio) — combinación pedida por
        # Juanjo, no probada todavía.
        if calif_directivas and not db.par_tiene_simulacion_combo_abierta(par):
            db.crear_simulacion_combo(par, candidato["direccion"], candidato.get("score"),
                                       candidato.get("adx"), candidato.get("atr_pct"), candidato["precio"])

        # 16/09 — 5ta simulación: "Real (fix28) fiel" — réplica exacta
        # de la estrategia real, misma entrada y misma salida (con las
        # 2 protecciones extra), sin capital real. A pedido de Juanjo:
        # recopilar qué HUBIERA pasado si el bot siguiera activo,
        # mientras está pausado, para comparar después.
        if not db.par_tiene_simulacion_fix28_fiel_abierta(par):
            db.crear_simulacion_fix28_fiel(par, candidato["direccion"], candidato.get("score"),
                                            candidato.get("adx"), candidato.get("atr_pct"), candidato["precio"],
                                            candidato.get("rango_bajo"), candidato.get("rango_alto"))

    # ── V5.2 — Ranking de Fuerza (AHORA EN SOMBRA): recién ACÁ, con los
    # 120 pares ya evaluados, se aplica el ranking de V5.0 y se recopila
    # en "V5.0 fiel" — YA NO abre con capital real desde el 24/09
    # (Directiva V5.5 la reemplazó). Sigue recopilando datos SIEMPRE,
    # sin importar la pausa, tal como pidió Juanjo explícitamente
    # ("siguen todas las estrategias vigentes y en modo sombra").
    mejores_v5 = aplicar_ranking_de_fuerza_v52(candidatos_v5_calificados)
    for candidato_v5 in mejores_v5:
        par = candidato_v5["par"]
        # "V5.0 fiel" — SIEMPRE recopila (de los 2 mejores), sin importar la pausa
        if not db.par_tiene_simulacion_v5_fiel_abierta(par):
            db.crear_simulacion_v5_fiel(par, candidato_v5["direccion"], candidato_v5.get("score"),
                                         candidato_v5.get("adx"), candidato_v5.get("atr_pct"), candidato_v5["precio"])
        # 24/09: ya NO se abre posición real acá — V5.0 quedó en modo sombra exclusivo.

    # ── 24/09 — Directiva V5.5 ("Estrategia Simplificada"): AHORA LA
    # REAL. Mismo pool de candidatos que V5.0 (candidatos_v5_calificados,
    # ya pasaron el filtro de universo + gates + ADX/RSI), pero con
    # ranking puro (sin pendiente) y su propia gestión de riesgo
    # (SL -25% apalancado / TP trailing +5% apalancado). Los 2 mejores
    # del ciclo son los que abren con capital real cuando el bot está
    # activo.
    mejores_v55 = aplicar_ranking_v55(candidatos_v5_calificados)
    for candidato_v55 in mejores_v55:
        par = candidato_v55["par"]
        # "V5.5" — SIEMPRE recopila (de los 2 mejores), sin importar la
        # pausa. 24/09: ahora respeta el MISMO tope que tendría operando
        # real — 6 simulaciones abiertas a la vez y 2 aperturas nuevas
        # por ciclo de 15min (medido contra su propia tabla de
        # simulación, no contra la real) — para que la sombra sea fiel
        # a cómo se comportaría el bot de verdad, no una lista libre sin
        # límite de posiciones simultáneas.
        if not db.par_tiene_simulacion_v55_abierta(par):
            lugar_sim = gestion_riesgo.hay_lugar_para_abrir_v55()
            if lugar_sim["hay_lugar"]:
                db.crear_simulacion_v55(par, candidato_v55["direccion"], candidato_v55.get("score"),
                                         candidato_v55.get("adx"), candidato_v55.get("atr_pct"), candidato_v55["precio"])

        if not pausado and not db.par_tiene_posicion_abierta(par):
            if not (modo_cauto_activo and db.contar_posiciones_por_direccion(candidato_v55["direccion"]) >= 3):
                lugar = gestion_riesgo.hay_lugar_para_abrir()
                if lugar["hay_lugar"]:
                    abrir_posicion_real(candidato_v55)


# ── 25/09 — Seguimiento post-cierre de V5.5 fiel (12hs) ─────────
def procesar_seguimiento_post_cierre_v55():
    """
    Para cada cierre de V5.5 fiel (SL, trailing, lo que sea) se sigue
    consultando el precio del par durante 12hs MÁS, en 6 checkpoints
    fijos (1/2/4/6/8/12hs desde el cierre) — a pedido de Juanjo, para
    poder ver objetivamente "qué hubiera pasado" con un SL más ancho,
    sin depender de reconstruir el historial después con una fuente
    externa (que además resultó no confiable para fechas específicas).

    No toca el resultado ya guardado de la simulación — esto es pura
    observación adicional en una tabla aparte. Corre en el mismo hilo
    de 2seg que el resto de los chequeos, así que el costo es mínimo
    (una consulta de precio por par pendiente, no por checkpoint).
    """
    pendientes = db.seguimientos_v55_pendientes()
    if not pendientes:
        return

    for seg in pendientes:
        try:
            creado_dt = datetime.fromisoformat(seg["creado"])
        except Exception:
            db.marcar_seguimiento_v55_terminado(seg["id"])  # dato corrupto, no reintentar para siempre
            continue

        horas_transcurridas = (datetime.now(TZ_ARG) - creado_dt).total_seconds() / 3600
        signo = 1 if seg["direccion"] == "LARGO" else -1

        checkpoint_pendiente = None
        for horas in db.CHECKPOINTS_SEGUIMIENTO_V55:
            ya_guardado = seg.get(f"precio_{horas}h") is not None
            if not ya_guardado and horas_transcurridas >= horas:
                checkpoint_pendiente = horas
                break  # solo 1 checkpoint por vuelta — si pasó mucho tiempo sin correr, los va completando de a uno

        if checkpoint_pendiente is not None:
            precio_actual = get_precio(seg["par"])
            if precio_actual is not None:
                # Mismo criterio que el resto del sistema: % de cambio de
                # precio SIEMPRE relativo a precio_entrada original (no
                # compuesto desde el precio de cierre) — así el checkpoint
                # es directamente comparable con resultado_cierre_pct.
                cambio_precio_pct = (precio_actual - seg["precio_entrada"]) / seg["precio_entrada"] * 100
                resultado_hipotetico_pct = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                db.guardar_checkpoint_seguimiento_v55(seg["id"], checkpoint_pendiente, precio_actual, resultado_hipotetico_pct)
                # 25/09 FIX (encontrado con test): solo se marca terminado
                # cuando efectivamente se completó el ÚLTIMO checkpoint
                # (12hs), no solo por haber pasado 12hs de reloj — si el
                # bot estuvo caído y el tiempo "saltó" de golpe, este
                # límite por tiempo cortaba el seguimiento habiendo
                # llenado solo 1 o 2 checkpoints, perdiendo el resto para
                # siempre. Así, aunque haya que "ponerse al día" de a un
                # checkpoint por vuelta (cada 2seg), ninguno se pierde.
                if checkpoint_pendiente == max(db.CHECKPOINTS_SEGUIMIENTO_V55):
                    db.marcar_seguimiento_v55_terminado(seg["id"])


# ── Chequeo rápido de SL/trailing — DIRECTO a Pionex, cada 2seg ────
def chequeo_rapido_riesgo():
    """
    Corre en threading.Thread aparte (daemon), totalmente independiente
    del ciclo de selección — mismo patrón que v18 (el escaneo de pares NO
    puede bloquear esto). Consulta Pionex directo, sin cascada.
    """
    ciclo_n = 0
    btc_cache = {"estado": None, "ciclo_actualizado": -999}
    while True:
        ciclo_n += 1
        try:
            # 10/09: estado de BTC cacheado, se refresca cada ~1 min (30
            # ciclos de 2seg) — no tiene sentido consultarlo cada 2seg,
            # BTC no cambia de tendencia en segundos, y ahorra llamadas
            # a la cascada externa sin ninguna pérdida real de precisión.
            if ciclo_n - btc_cache["ciclo_actualizado"] >= 30:
                try:
                    btc_cache["estado"] = analizar_btc()["estado"]
                    btc_cache["ciclo_actualizado"] = ciclo_n
                except Exception:
                    pass  # se sigue usando el valor cacheado anterior si falla

            abiertas = db.posiciones_abiertas()
            if ciclo_n % 30 == 1:  # print de "sigo vivo" cada ~1 min (30 ciclos de 2seg), no cada 2seg (no saturar logs)
                print(f"🔄 chequeo_rapido_riesgo activo (ciclo {ciclo_n}) — {len(abiertas)} posición(es) abierta(s)", flush=True)
            for senal in abiertas:
                # 11/09 FIX CRÍTICO: se cambió de la cascada externa a
                # Pionex directo (endpoint público /market/tickers) —
                # un precio corrupto de la cascada puede disparar un
                # cierre real prematuro (caso real: BLURUSDT, cascada
                # dio un precio ~29% distinto del real por un instante).
                # La cascada queda solo como respaldo de emergencia si
                # Pionex directo falla puntualmente.
                precio_actual = pionex_api.obtener_precio_pionex_directo(senal["par"])
                if precio_actual is None:
                    precio_actual = get_precio(senal["par"])
                    if precio_actual is not None:
                        print(f"⚠️ chequeo_rapido_riesgo: Pionex directo falló para {senal['par']}, usando cascada de respaldo")
                if precio_actual is None:
                    print(f"⚠️ chequeo_rapido_riesgo: no se pudo obtener precio (ni Pionex directo ni cascada) para {senal['par']} — se salta este ciclo")
                    continue
                resultado_pct = pionex_api.calcular_resultado_actual(senal["bu_order_id"], precio_actual)
                if resultado_pct is None:
                    print(f"⚠️ chequeo_rapido_riesgo: resultado_pct=None para {senal['par']} (bu_order_id={senal['bu_order_id']}) — revisar con /debug_orden")
                    continue

                db.actualizar_mae_mfe(senal["id"], resultado_pct)

                # 24/09 — Directiva V5.5: las posiciones REALES ahora
                # usan la salida de V5.5 (SL -25% apalancado / -2,5%
                # real + trailing 1 tramo, activa en +5% apalancado),
                # reemplazando a la salida de V5.0 (SL -4,5%, que venía
                # mostrando "asfixia" — cierres por ruido antes de que
                # la posición pudiera desarrollarse). evaluar_cierre_v5
                # sigue existiendo intacta, usada por la simulación
                # "V5.0 fiel" (ahora en sombra).
                pico_actual_senal = senal.get("pico_maximo_pct", 0) or 0
                decision_v55 = gestion_riesgo.evaluar_cierre_v55(senal["direccion"], pico_actual_senal, resultado_pct)
                db.actualizar_pico_y_tramo(senal["id"], decision_v55["pico_nuevo"], "unico", decision_v55["pico_nuevo"] >= gestion_riesgo.PICO_ACTIVACION_V55_PCT)
                decision = decision_v55
                if decision["cerrar"]:
                    cierre = pionex_api.cerrar_grilla_futuros(senal["bu_order_id"], nota=decision["motivo"])
                    if not cierre["ok"]:
                        # 04/09 FIX CRÍTICO: si Pionex rechazó el cierre, la
                        # posición NUNCA se marca como cerrada acá — sigue en
                        # posiciones_abiertas() y se reintenta cerrar en el
                        # próximo ciclo (2seg). Antes esto quedaba invisible
                        # y la posición real seguía corriendo sin control.
                        #
                        # 05/09 FIX (nuevo, encontrado en producción): esa
                        # regla de "nunca marcar cerrado" era demasiado
                        # amplia — si el motivo del rechazo es
                        # BOT_ORDER_ALREADY_CLOSED, la posición YA está
                        # cerrada de verdad en Pionex (por el SL nativo u
                        # otro medio), no es un fallo real. Sin este caso
                        # aparte, el bot reintentaba cerrar la misma
                        # posición cada 2seg PARA SIEMPRE, generando spam
                        # infinito de avisos sin ningún riesgo real de
                        # capital de por medio (la posición ya estaba a
                        # salvo, solo desactualizada en nuestra base).
                        codigo_error = cierre["resultado"].get("code") if isinstance(cierre["resultado"], dict) else None
                        if codigo_error == "BOT_ORDER_ALREADY_CLOSED":
                            print(f"ℹ️ {senal['par']}: ya cerrada en Pionex, consultando resultado final...", flush=True)
                            resultado_final = resultado_pct  # fallback por si consultar_orden falla o tarda
                            try:
                                estado_real = pionex_api.esta_cerrada(senal["bu_order_id"])
                                if estado_real.get("resultado_pct") is not None:
                                    resultado_final = estado_real["resultado_pct"]
                            except Exception as e:
                                print(f"⚠️ No se pudo consultar el resultado final de {senal['par']} (uso la última lectura): {e}", flush=True)
                            db.cerrar_senal(senal["id"], resultado_final, f"{decision['motivo']}_ya_cerrada_en_pionex")
                            print(f"✅ {senal['par']} marcada como cerrada en nuestra base (resultado {resultado_final:+.2f}%).", flush=True)
                            try:
                                telegram_cmds.enviar(
                                    f"ℹ️ <b>{senal['par']}</b>: ya estaba cerrada en Pionex (probablemente por el SL nativo) "
                                    f"— actualizado en nuestra base. Resultado: {resultado_final:+.2f}%"
                                )
                            except Exception as e:
                                print(f"⚠️ No se pudo avisar por Telegram (pero SÍ quedó cerrada en nuestra base): {e}", flush=True)
                            continue

                        try:
                            telegram_cmds.enviar(
                                f"🚨 <b>{senal['par']}: Pionex RECHAZÓ el cierre</b> (motivo: {decision['motivo']})\n"
                                f"Resultado calculado: {resultado_pct:+.2f}% | Reintentando cada 2seg — "
                                f"si esto persiste, CERRAR MANUALMENTE en la app.\n"
                                f"<code>{str(cierre['resultado'])[:250]}</code>"
                            )
                        except Exception as e:
                            print(f"⚠️ No se pudo avisar del rechazo de {senal['par']} por Telegram: {e}", flush=True)
                        continue
                    db.cerrar_senal(senal["id"], resultado_pct, decision["motivo"])
                    print(f"✅ {senal['par']} cerrado normal ({decision['motivo']}): {resultado_pct:+.2f}%", flush=True)
                    try:
                        telegram_cmds.enviar(
                            f"{'🟢' if resultado_pct > 0 else '🔴'} <b>{senal['par']} cerrado</b> ({decision['motivo']})\n"
                            f"Resultado: {resultado_pct:+.2f}%"
                        )
                    except Exception as e:
                        print(f"⚠️ No se pudo avisar del cierre de {senal['par']} por Telegram (pero sí quedó cerrada en nuestra base): {e}", flush=True)

            # ── 13/09: chequeo de simulaciones (sin capital real) ──
            for sim in db.simulaciones_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_simulado(sim["direccion"], sim["atr_pct"], sim["pico_maximo_pct"], resultado_actual_sim)
                if decision_sim["cerrar"]:
                    db.cerrar_simulacion(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_pico_simulacion(sim["id"], decision_sim["pico_nuevo"])

            # ── 14/09: chequeo de simulaciones de Directivas (sin capital real) ──
            for sim in db.simulaciones_directivas_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_directivas(sim["direccion"], sim["pico_maximo_pct"], resultado_actual_sim)
                if decision_sim["cerrar"]:
                    db.cerrar_simulacion_directivas(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_pico_simulacion_directivas(sim["id"], decision_sim["pico_nuevo"])

            # ── 16/09: chequeo de la 4ta simulación (combo: entrada Directivas + salida original) ──
            for sim in db.simulaciones_combo_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_simulado(sim["direccion"], sim["atr_pct"], sim["pico_maximo_pct"], resultado_actual_sim)
                if decision_sim["cerrar"]:
                    db.cerrar_simulacion_combo(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_pico_simulacion_combo(sim["id"], decision_sim["pico_nuevo"])

            # ── 16/09: chequeo de la 5ta simulación ("Real (fix28) fiel") ──
            for sim in db.simulaciones_fix28_fiel_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_fix28_fiel(
                    sim["direccion"], sim["atr_pct"], sim["pico_maximo_pct"], resultado_actual_sim,
                    precio_actual_sim, sim.get("rango_bajo"), sim.get("rango_alto"),
                    sim.get("fuera_rango_desde"), btc_cache["estado"]
                )
                if decision_sim["cerrar"]:
                    db.cerrar_simulacion_fix28_fiel(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_simulacion_fix28_fiel(sim["id"], decision_sim["pico_nuevo"], decision_sim["fuera_rango_desde_nuevo"])

            # ── 17/09: chequeo de "V5.0 fiel" (la NUEVA principal, sin capital real) ──
            for sim in db.simulaciones_v5_fiel_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_v5(sim["direccion"], sim["pico_maximo_pct"], resultado_actual_sim)
                if decision_sim["cerrar"]:
                    db.cerrar_simulacion_v5_fiel(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_pico_simulacion_v5_fiel(sim["id"], decision_sim["pico_nuevo"])

            # ── 24/09: chequeo de "V5.5" (la NUEVA principal, sin capital real —
            # esta tabla además alimenta directamente a la posición real, que
            # usa evaluar_cierre_v55 arriba con datos consultados directo a Pionex) ──
            for sim in db.simulaciones_v55_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_v55(sim["direccion"], sim["pico_maximo_pct"], resultado_actual_sim)
                if decision_sim["cerrar"]:
                    db.cerrar_simulacion_v55(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                    # 25/09 — Arranca el seguimiento post-cierre (12hs, checkpoints
                    # fijos): a pedido de Juanjo, para saber objetivamente cómo
                    # siguió cotizando el par después de cerrar, sin depender de
                    # reconstruir el historial después con una fuente externa.
                    db.crear_seguimiento_v55(sim["id"], sim["par"], sim["direccion"], sim["precio_entrada"],
                                              precio_actual_sim, resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_pico_simulacion_v55(sim["id"], decision_sim["pico_nuevo"])

            # ── 26/09 — Directiva (v2): chequeo de sombra_ranked_v55 —
            # candidatos de posición 1..TOP_SOMBRA_RANKED_V55 de cada ciclo,
            # simulados con la MISMA lógica de salida real (evaluar_cierre_v55),
            # sin tope. Esto es lo que permite backtestear después CUALQUIER
            # combinación de aperturas/tope con resultados de fidelidad
            # completa (ver informe_combo_v55 / /informe_combo).
            for sim in db.sombra_ranked_v55_abiertas():
                precio_actual_sim = get_precio(sim["par"])
                if precio_actual_sim is None:
                    continue
                cambio_precio_pct = (precio_actual_sim - sim["precio_entrada"]) / sim["precio_entrada"] * 100
                signo = 1 if sim["direccion"] == "LARGO" else -1
                resultado_actual_sim = cambio_precio_pct * signo * gestion_riesgo.LEVERAGE_FIJO
                decision_sim = gestion_riesgo.evaluar_cierre_v55(sim["direccion"], sim["pico_maximo_pct"], resultado_actual_sim)
                if decision_sim["cerrar"]:
                    db.cerrar_sombra_ranked_v55(sim["id"], resultado_actual_sim, decision_sim["motivo"])
                else:
                    db.actualizar_pico_sombra_ranked_v55(sim["id"], decision_sim["pico_nuevo"])

            # ── 25/09: seguimiento post-cierre de V5.5 fiel — registra el precio
            # en checkpoints fijos (1/2/4/6/8/12hs) para cada cierre, sin afectar
            # el resultado ya guardado. Corre en el mismo hilo de 2seg. ──
            procesar_seguimiento_post_cierre_v55()
        except Exception as e:
            print(f"⚠️ chequeo_rapido_riesgo: {e}", flush=True)
        time.sleep(2)


# ── Huérfanas — cada 30 min ──────────────────────────────────
def chequear_huerfanas():
    try:
        reales = pionex_api.listar_grillas_abiertas()  # ya devuelve la lista filtrada
        ids_reales = {str(g.get("buOrderId")) for g in reales if g.get("buOrderId")}

        nuestras = db.posiciones_abiertas()
        for senal in nuestras:
            if str(senal["bu_order_id"]) not in ids_reales:
                telegram_cmds.enviar(
                    f"👻 <b>Posible huérfana</b>: {senal['par']} (id {senal['id']}) figura abierta en "
                    f"nuestra base pero NO aparece en la lista real de Pionex — REVISAR manualmente."
                )
    except Exception as e:
        print(f"⚠️ chequear_huerfanas: {e}")


# ── Capital diario — 00:01 ARG, reintenta cada 1 min si hay abiertas ──
def recalculo_diario_job():
    resultado = gestion_riesgo.intentar_recalculo_diario()
    if resultado:
        telegram_cmds.enviar(resultado)


# ── Arranque ─────────────────────────────────────────────────
def main():
    db.init_db()
    telegram_cmds.inicializar_offset_telegram()
    telegram_cmds.enviar("🤖 <b>Bot Cripto v2</b> arrancó — rediseño desde cero (04/09/2026).")

    hilo_riesgo = threading.Thread(target=chequeo_rapido_riesgo, daemon=True)
    hilo_riesgo.start()

    schedule.every(15).minutes.do(ciclo_seleccion)
    schedule.every(30).minutes.do(chequear_huerfanas)
    schedule.every().day.at("00:01").do(recalculo_diario_job)
    schedule.every(1).minutes.do(lambda: gestion_riesgo.intentar_recalculo_diario() if db.contar_posiciones_abiertas() == 0 and not db.obtener_capital_diario() else None)

    ciclo_principal = 0
    while True:
        ciclo_principal += 1
        try:
            schedule.run_pending()
            telegram_cmds.revisar_updates()
        except Exception as e:
            # 05/09 FIX CRÍTICO: este loop principal (el que escucha tus
            # comandos de Telegram y dispara los ciclos programados) NO
            # tenía ningún manejo de errores — un fallo acá se colgaba en
            # silencio total, para siempre, sin ningún aviso ni reinicio.
            # Caso real: el 05/09 el bot dejó de responder /pausar_todo
            # desde las 10:57 sin ningún rastro visible del motivo.
            print(f"⚠️ loop principal: {e}", flush=True)
        if ciclo_principal % 12 == 1:  # print de "sigo vivo" cada ~1 min (12 ciclos de 5seg)
            print(f"💓 loop principal activo (ciclo {ciclo_principal})", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
