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
    "1000PEPEUSDT", "WIFUSDT", "FLOKIUSDT",
    "ENAUSDT", "TIAUSDT", "NOTUSDT", "TAOUSDT",
    "ORDIUSDT", "ACEUSDT", "ALTUSDT", "PORTALUSDT",
    "APTUSDT", "ARKMUSDT", "BLURUSDT", "GMTUSDT", "IMXUSDT",
    "JASMYUSDT", "JTOUSDT", "KASUSDT", "MASKUSDT",
    "ONDOUSDT", "PYTHUSDT", "ROSEUSDT", "SSVUSDT",
    "STRKUSDT", "SUPERUSDT", "TWTUSDT", "UMAUSDT", "WUSDT",
    "XAIUSDT", "ZETAUSDT", "ZRXUSDT",
    "TONUSDT", "EIGENUSDT", "MOVEUSDT", "VIRTUALUSDT",
    "PENGUUSDT", "MOCAUSDT", "SCRUSDT",
]

# Pares "majors" — umbral de ADX más bajo (23 en vez de 28), porque
# sostienen tendencias más largas que los altcoins.
PARES_MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}

SCORE_MAX = 10
SCORE_UMBRAL = 7
SCORE_MOMENTUM_TOPE = 4  # familia RSI+StochRSI+MACD+Bollinger+vela, topeada
VOLUMEN_RATIO_MINIMO = 1.5
FUNDING_UMBRAL_PCT = 0.05  # ±0.05%/8h — a calibrar con datos reales

HORA_INICIO, HORA_FIN = 7, 23  # horario operativo ARG


def hoy_arg():
    return datetime.now(TZ_ARG).strftime("%Y%m%d")


def en_horario_operativo() -> bool:
    return HORA_INICIO <= datetime.now(TZ_ARG).hour < HORA_FIN


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
    return {"adx": float(adx.iloc[-1]), "plus_di": float(plus_di.iloc[-1]), "minus_di": float(minus_di.iloc[-1])}


def patron_vela_score(df) -> int:
    """Patrón de vela simple (0, 1 o 2 puntos) — parte de la familia momentum topeada."""
    c, o = df["close"].iloc[-1], df["open"].iloc[-1]
    h, l = df["high"].iloc[-1], df["low"].iloc[-1]
    cuerpo = abs(c - o)
    rango_total = h - l
    if rango_total <= 0:
        return 0
    if cuerpo / rango_total > 0.6:
        return 2  # vela con cuerpo fuerte, dirección clara
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
    df = get_velas("BTCUSDT", "1h", 100)
    if df is None:
        return {"estado": "SIN_DATO", "cambio_1h_pct": 0}
    ema9 = calc_ema(df["close"], 9)
    ema21 = calc_ema(df["close"], 21)
    cambio_1h_pct = (df["close"].iloc[-1] - df["close"].iloc[-2]) / df["close"].iloc[-2] * 100
    if ema9 > ema21 * 1.001:
        estado = "ALCISTA"
    elif ema9 < ema21 * 0.999:
        estado = "BAJISTA"
    else:
        estado = "LATERAL"
    return {"estado": estado, "cambio_1h_pct": round(cambio_1h_pct, 3)}


# ── Análisis de un par: 3 gates + score ─────────────────────
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

    ema20_4h = calc_ema(df4h["close"], 20)
    ema9_15m = calc_ema(df15["close"], 9)

    # Dirección candidata: EMA9(15m) vs precio, confirmada por DI
    direccion = "LARGO" if precio > ema9_15m else "CORTO"

    # ── GATE 1: ADX + DI (umbral diferenciado por tipo de par) ──
    adx_umbral = 23 if par in PARES_MAJORS else 28
    di_confirma = (plus_di > minus_di) if direccion == "LARGO" else (minus_di > plus_di)
    paso_adx = adx > adx_umbral and di_confirma
    if not paso_adx:
        db.guardar_gates_log(par, direccion, adx, adx_umbral, False, False, False, 0, 0, False)
        return None

    # ── GATE 2: alineación EMA20 4h ──
    paso_ema4h = (precio > ema20_4h) if direccion == "LARGO" else (precio < ema20_4h)
    if not paso_ema4h:
        db.guardar_gates_log(par, direccion, adx, adx_umbral, True, False, False, 0, 0, False)
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
        db.guardar_gates_log(par, direccion, adx, adx_umbral, True, True, False, 0, 0, False)
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

    score_momentum_bruto += patron_vela_score(df15)

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

    # Confirmación 1h
    ema9_1h = calc_ema(df1h["close"], 9)
    if (direccion == "LARGO" and precio > ema9_1h) or (direccion == "CORTO" and precio < ema9_1h):
        score_independiente += 1; razones.append("Confirma en 1h")

    # Volumen (umbral 1.5x)
    vol_prom = df15["vol"].iloc[-21:-1].mean()
    vol_actual = df15["vol"].iloc[-1]
    if vol_prom > 0 and vol_actual / vol_prom >= VOLUMEN_RATIO_MINIMO:
        score_independiente += 1; razones.append(f"Volumen {vol_actual/vol_prom:.1f}x")

    score_total = score_momentum + score_independiente
    califico = score_total >= SCORE_UMBRAL

    db.guardar_gates_log(par, direccion, adx, adx_umbral, True, True, True,
                          score_total, score_momentum, califico)

    if not califico:
        return None

    grid = calcular_grid(precio, atr_pct, adx)

    return {
        "par": par, "direccion": direccion, "precio": precio,
        "adx": round(adx, 2), "adx_umbral_usado": adx_umbral, "di_confirma": di_confirma,
        "ema4h_alineada": paso_ema4h, "funding_rate": funding, "funding_bloqueo": False,
        "score": score_total, "score_momentum": score_momentum, "razones": razones,
        "atr_pct": round(atr_pct, 3),
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
    if db.esta_pausado_global():
        return
    if not en_horario_operativo():
        return

    lugar = gestion_riesgo.hay_lugar_para_abrir()
    if not lugar["hay_lugar"]:
        return

    btc = analizar_btc()
    for par in PARES:
        if db.par_tiene_posicion_abierta(par):
            continue
        lugar = gestion_riesgo.hay_lugar_para_abrir()
        if not lugar["hay_lugar"]:
            break
        try:
            candidato = analizar_par(par, btc)
        except Exception as e:
            print(f"Error analizando {par}: {e}")
            continue
        if candidato:
            abrir_posicion_real(candidato)


# ── Chequeo rápido de SL/trailing — DIRECTO a Pionex, cada 2seg ────
def chequeo_rapido_riesgo():
    """
    Corre en threading.Thread aparte (daemon), totalmente independiente
    del ciclo de selección — mismo patrón que v18 (el escaneo de pares NO
    puede bloquear esto). Consulta Pionex directo, sin cascada.
    """
    while True:
        try:
            abiertas = db.posiciones_abiertas()
            for senal in abiertas:
                resultado_pct = pionex_api.calcular_resultado_actual(senal["bu_order_id"])
                if resultado_pct is None:
                    print(f"⚠️ chequeo_rapido_riesgo: resultado_pct=None para {senal['par']} (bu_order_id={senal['bu_order_id']}) — revisar con /debug_orden")
                    continue

                db.actualizar_mae_mfe(senal["id"], resultado_pct)

                decision = gestion_riesgo.evaluar_cierre(senal, resultado_pct)
                if decision["cerrar"]:
                    cierre = pionex_api.cerrar_grilla_futuros(senal["bu_order_id"], nota=decision["motivo"])
                    db.cerrar_senal(senal["id"], resultado_pct, decision["motivo"])
                    telegram_cmds.enviar(
                        f"{'🟢' if resultado_pct > 0 else '🔴'} <b>{senal['par']} cerrado</b> ({decision['motivo']})\n"
                        f"Resultado: {resultado_pct:+.2f}%"
                    )
        except Exception as e:
            print(f"⚠️ chequeo_rapido_riesgo: {e}")
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

    while True:
        schedule.run_pending()
        telegram_cmds.revisar_updates()
        time.sleep(5)


if __name__ == "__main__":
    main()
