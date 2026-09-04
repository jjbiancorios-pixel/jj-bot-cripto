"""
db.py — Bot Cripto (rediseño desde cero)
──────────────────────────────────────────
Persistencia SQLite en Railway Volume (/data/bot.db).

Diseño de referencia (JJ_Cripto_Bot_Rediseno_BotCripto.docx +
JJ_Cripto_Bot_Rediseno_BotCripto_EntradaV2.docx):
  - Entrada: 3 gates (ADX+DI diferenciado, EMA20 4h, funding rate) +
    score máx 10 (umbral 7, familia momentum topeada a 4pts)
  - Riesgo: SL fijo 4%, trailing TP por pico (breakeven 0-1%, luego
    retrocesos 50/30/20%)
  - Grilla: rango ATR%×3 con piso por ADX, cantidad recomendada por Pionex
  - Capital: interés compuesto diario (recalcula 00:01 ARG), 5% por
    posición, sin reserva
  - 6 posiciones simultáneas, máx 2 aperturas/15min
"""
import sqlite3
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", "/data/bot.db")  # /data = Volume de Railway
TZ_ARG = timezone(timedelta(hours=-3))


def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Crea las tablas si no existen. Llamar una vez al iniciar el bot."""
    conn = _conn()
    cur = conn.cursor()

    # Señales/posiciones — histórico completo, real y simulado
    cur.execute("""
        CREATE TABLE IF NOT EXISTS senales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora_alerta TEXT NOT NULL,

            -- Entrada (gates + score, sistema nuevo)
            adx REAL,
            adx_umbral_usado REAL,
            di_confirma INTEGER,
            ema4h_alineada INTEGER,
            funding_rate REAL,
            funding_bloqueo INTEGER DEFAULT 0,
            score INTEGER,
            score_momentum INTEGER,
            score_max INTEGER DEFAULT 10,
            razones TEXT,

            -- Grilla calculada
            precio_entrada REAL,
            atr_pct REAL,
            rango_pct REAL,
            rango_bajo REAL,
            rango_alto REAL,
            grillas INTEGER,

            -- Ejecución real en Pionex
            bu_order_id TEXT,
            capital_asignado REAL,
            leverage INTEGER DEFAULT 10,
            registrado_pionex INTEGER DEFAULT 0,

            -- Riesgo / seguimiento
            sl_pct REAL DEFAULT -4.0,
            breakeven_activo INTEGER DEFAULT 0,
            pico_maximo_pct REAL DEFAULT 0,
            tramo_trailing_actual TEXT,

            -- Resultado
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            tiempo_real_min INTEGER,
            hora_cierre TEXT,
            peor_resultado_pct REAL,
            mejor_resultado_pct REAL,

            creado TEXT NOT NULL
        )
    """)

    # Capital diario — interés compuesto (00:01 ARG, sin reserva)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS capital_diario (
            fecha TEXT PRIMARY KEY,
            capital_dia REAL NOT NULL,
            tamano_objetivo REAL NOT NULL,
            creado TEXT NOT NULL
        )
    """)

    # Config general (pausa global, etc.)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT
        )
    """)

    # Log de detalle de gates por ciclo (para diagnosticar sin ventana de sombra previa)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS gates_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            adx REAL,
            adx_umbral_usado REAL,
            paso_adx INTEGER,
            paso_ema4h INTEGER,
            paso_funding INTEGER,
            score INTEGER,
            score_momentum INTEGER,
            califico INTEGER,
            creado TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


# ── Pausa global ─────────────────────────────────────────────
def pausar_todo(motivo: str = ""):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_global', '1')")
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_motivo', ?)", (motivo,))
    conn.commit()
    conn.close()


def reanudar_todo():
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('pausado_global', '0')")
    conn.commit()
    conn.close()


def esta_pausado_global() -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE clave = 'pausado_global'")
    row = cur.fetchone()
    conn.close()
    return bool(row and row[0] == "1")


# ── Gates log (diagnóstico detallado desde el día 1) ────────
def guardar_gates_log(par: str, direccion: str, adx: float, adx_umbral_usado: float,
                       paso_adx: bool, paso_ema4h: bool, paso_funding: bool,
                       score: int, score_momentum: int, califico: bool):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO gates_log
            (par, direccion, fecha, hora, adx, adx_umbral_usado, paso_adx, paso_ema4h,
             paso_funding, score, score_momentum, califico, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), adx, adx_umbral_usado,
          int(paso_adx), int(paso_ema4h), int(paso_funding), score, score_momentum, int(califico),
          ahora.isoformat()))
    conn.commit()
    conn.close()


# ── Señales ──────────────────────────────────────────────────
def guardar_senal(r: dict) -> int:
    """Guarda una señal recién generada (ya pasó los 3 gates + score). Devuelve el id."""
    import json
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    razones_json = json.dumps(r.get("razones", []), ensure_ascii=False)
    cur.execute("""
        INSERT INTO senales (
            par, direccion, fecha, hora_alerta,
            adx, adx_umbral_usado, di_confirma, ema4h_alineada, funding_rate, funding_bloqueo,
            score, score_momentum, razones,
            precio_entrada, atr_pct, rango_pct, rango_bajo, rango_alto, grillas,
            creado
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        r["par"], r["direccion"], ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"),
        r.get("adx"), r.get("adx_umbral_usado"), int(r.get("di_confirma", False)),
        int(r.get("ema4h_alineada", False)), r.get("funding_rate"), int(r.get("funding_bloqueo", False)),
        r["score"], r.get("score_momentum"), razones_json,
        r.get("precio"), r.get("atr_pct"), r.get("rango_pct"), r.get("rango_bajo"), r.get("rango_alto"),
        r.get("grillas"), ahora.isoformat(),
    ))
    conn.commit()
    senal_id = cur.lastrowid
    conn.close()
    return senal_id


def guardar_bu_order_id(senal_id: int, bu_order_id: str, capital_asignado: float, leverage: int = 10):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET bu_order_id = ?, capital_asignado = ?, leverage = ?, registrado_pionex = 1
        WHERE id = ?
    """, (bu_order_id, capital_asignado, leverage, senal_id))
    conn.commit()
    conn.close()


def posiciones_abiertas() -> list:
    """Todas las posiciones reales abiertas (con bu_order_id, sin cerrar)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM senales WHERE cerrado = 0 AND bu_order_id IS NOT NULL
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def contar_posiciones_abiertas() -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM senales WHERE cerrado = 0 AND bu_order_id IS NOT NULL")
    n = cur.fetchone()[0]
    conn.close()
    return n


def contar_aperturas_ultimos_minutos(minutos: int = 15) -> int:
    """Cuántas posiciones se abrieron en los últimos N minutos (para el tope de 2 por ciclo de 15min)."""
    conn = _conn()
    cur = conn.cursor()
    limite = (datetime.now(TZ_ARG) - timedelta(minutes=minutos)).isoformat()
    cur.execute("""
        SELECT COUNT(*) FROM senales WHERE bu_order_id IS NOT NULL AND creado >= ?
    """, (limite,))
    n = cur.fetchone()[0]
    conn.close()
    return n


def par_tiene_posicion_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*) FROM senales WHERE par = ? AND cerrado = 0 AND bu_order_id IS NOT NULL
    """, (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def actualizar_pico_y_tramo(senal_id: int, pico_nuevo: float, tramo: str, breakeven_activo: bool):
    """Actualiza el pico máximo histórico (nunca baja) y el tramo de trailing vigente."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET
            pico_maximo_pct = CASE WHEN ? > pico_maximo_pct THEN ? ELSE pico_maximo_pct END,
            tramo_trailing_actual = ?,
            breakeven_activo = ?
        WHERE id = ?
    """, (pico_nuevo, pico_nuevo, tramo, int(breakeven_activo), senal_id))
    conn.commit()
    conn.close()


def actualizar_mae_mfe(senal_id: int, resultado_actual: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE senales SET
            peor_resultado_pct = CASE WHEN peor_resultado_pct IS NULL OR ? < peor_resultado_pct THEN ? ELSE peor_resultado_pct END,
            mejor_resultado_pct = CASE WHEN mejor_resultado_pct IS NULL OR ? > mejor_resultado_pct THEN ? ELSE mejor_resultado_pct END
        WHERE id = ?
    """, (resultado_actual, resultado_actual, resultado_actual, resultado_actual, senal_id))
    conn.commit()
    conn.close()


def cerrar_senal(senal_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT hora_alerta, fecha FROM senales WHERE id = ?", (senal_id,))
    row = cur.fetchone()
    tiempo_real_min = None
    if row:
        try:
            apertura = datetime.strptime(f"{row['fecha']} {row['hora_alerta']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            tiempo_real_min = int((datetime.now(TZ_ARG) - apertura).total_seconds() / 60)
        except Exception:
            pass
    cur.execute("""
        UPDATE senales SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?,
                            tiempo_real_min = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, tiempo_real_min, datetime.now(TZ_ARG).strftime("%H:%M"), senal_id))
    conn.commit()
    conn.close()


# ── Capital diario (interés compuesto, sin reserva) ─────────
def guardar_capital_diario(capital_dia: float, tamano_objetivo: float):
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("""
        INSERT OR REPLACE INTO capital_diario (fecha, capital_dia, tamano_objetivo, creado)
        VALUES (?,?,?,?)
    """, (hoy, capital_dia, tamano_objetivo, datetime.now(TZ_ARG).isoformat()))
    conn.commit()
    conn.close()


def obtener_capital_diario():
    """Devuelve el registro de HOY o None si el recálculo de las 00:01 todavía no corrió."""
    conn = _conn()
    cur = conn.cursor()
    hoy = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("SELECT * FROM capital_diario WHERE fecha = ?", (hoy,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ── Resúmenes básicos ────────────────────────────────────────
def resumen_diario(fecha: str = None) -> dict:
    conn = _conn()
    cur = conn.cursor()
    if fecha is None:
        fecha = datetime.now(TZ_ARG).strftime("%Y%m%d")
    cur.execute("SELECT * FROM senales WHERE fecha = ? AND bu_order_id IS NOT NULL", (fecha,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    cerradas = [r for r in rows if r["cerrado"] == 1 and r["resultado_pct"] is not None]
    abiertas = [r for r in rows if r["cerrado"] == 0]
    ganadoras = [r for r in cerradas if r["resultado_pct"] > 0]

    return {
        "fecha": fecha,
        "n_cerradas": len(cerradas),
        "n_abiertas": len(abiertas),
        "n_ganadoras": len(ganadoras),
        "n_perdedoras": len(cerradas) - len(ganadoras),
        "win_rate_pct": round(len(ganadoras) / len(cerradas) * 100, 1) if cerradas else None,
        "resultado_prom_pct": round(sum(r["resultado_pct"] for r in cerradas) / len(cerradas), 2) if cerradas else None,
    }
