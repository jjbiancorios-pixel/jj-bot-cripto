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


def _migrar_columnas_nuevas(cur):
    """
    07-10/09 — Agrega columnas nuevas a una tabla `senales` que ya existe
    en producción (ALTER TABLE, no rompe los datos ya guardados).
    """
    columnas_nuevas = [
        ("fuera_rango_desde", "TEXT"),
        ("fecha_cierre", "TEXT"),
    ]
    for nombre, tipo in columnas_nuevas:
        try:
            cur.execute(f"ALTER TABLE senales ADD COLUMN {nombre} {tipo}")
        except Exception:
            pass  # ya existe

    columnas_gates_log_nuevas = [
        ("atr_pct", "REAL"),
        ("rsi", "REAL"),
        ("volumen_ratio", "REAL"),
        ("estrategia", "TEXT"),
    ]
    for nombre, tipo in columnas_gates_log_nuevas:
        try:
            cur.execute(f"ALTER TABLE gates_log ADD COLUMN {nombre} {tipo}")
        except Exception:
            pass  # ya existe


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
            fuera_rango_desde TEXT,

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

    _migrar_columnas_nuevas(cur)
    _crear_tabla_candidatos_v55_ciclo(cur)
    _crear_tabla_sombra_ranked_v55(cur)
    _crear_tabla_simulaciones(cur)  # su backfill de capital_asignado necesita sombra_ranked_v55 ya creada

    conn.commit()
    conn.close()


# ── Pausa global ─────────────────────────────────────────────
def actualizar_fuera_rango(senal_id: int, desde_iso: str = None):
    """desde_iso=None -> resetea (volvió a estar dentro de rango o nunca salió)."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE senales SET fuera_rango_desde = ? WHERE id = ?", (desde_iso, senal_id))
    conn.commit()
    conn.close()


def obtener_estado_btc_cauto() -> dict:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT valor FROM config WHERE clave = 'btc_modo_cauto'")
    row = cur.fetchone()
    conn.close()
    return {"activo": bool(row and row[0] == "1")}


def guardar_estado_btc_cauto(activo: bool):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO config (clave, valor) VALUES ('btc_modo_cauto', ?)", ("1" if activo else "0",))
    conn.commit()
    conn.close()


def contar_posiciones_por_direccion(direccion: str) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM senales WHERE cerrado = 0 AND bu_order_id IS NOT NULL AND direccion = ?", (direccion,))
    n = cur.fetchone()[0]
    conn.close()
    return n


def _crear_tabla_simulaciones(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            score INTEGER,
            adx REAL,
            atr_pct REAL,
            precio_entrada REAL,
            pico_maximo_pct REAL DEFAULT 0,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)
    # 14/09 — tabla nueva para "JJ_Cripto_Bot_Directivas_Optimizacion.pdf", misma estructura
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones_directivas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            score INTEGER,
            adx REAL,
            atr_pct REAL,
            precio_entrada REAL,
            pico_maximo_pct REAL DEFAULT 0,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)
    # 16/09 — 4ta simulación: entrada de Directivas (ADX≤30 LARGO +
    # bloqueo sobrecompra) combinada con la SALIDA de la simulación
    # original (SL -7.5% + trailing 3 tramos por ATR, ya validada con
    # backtest propio) — combinación que todavía no se había probado.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones_combo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            score INTEGER,
            adx REAL,
            atr_pct REAL,
            precio_entrada REAL,
            pico_maximo_pct REAL DEFAULT 0,
            capital_asignado REAL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)
    # 16/09 — simulación "Real (fix28) fiel": réplica exacta de la
    # estrategia real (misma entrada Y misma salida, con las 2
    # protecciones extra incluidas) sin capital real — a pedido de
    # Juanjo, para ver los datos "del bot tal cual está predeterminado
    # ahora" en la comparación, aunque esté pausado.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones_fix28_fiel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            score INTEGER,
            adx REAL,
            atr_pct REAL,
            precio_entrada REAL,
            rango_bajo REAL,
            rango_alto REAL,
            pico_maximo_pct REAL DEFAULT 0,
            fuera_rango_desde TEXT,
            capital_asignado REAL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    # 17/09 — Directiva V5.0: "V5.0 fiel" — réplica exacta de la NUEVA
    # estrategia real (reemplaza a fix28 en el capital de verdad).
    # Esquema simple (sin fuera_rango/BTC-en-contra — V5.0 no las
    # mantiene). Recopila SIEMPRE, sin importar la pausa.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones_v5_fiel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            score INTEGER,
            adx REAL,
            atr_pct REAL,
            precio_entrada REAL,
            pico_maximo_pct REAL DEFAULT 0,
            capital_asignado REAL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    # 25/09 — Seguimiento post-cierre de V5.5 fiel: cuando una simulación
    # cierra (SL, trailing, lo que sea), se sigue registrando el precio
    # del par durante 12hs MÁS, en checkpoints fijos (1h/2h/4h/6h/8h/12h),
    # para poder ver objetivamente "qué hubiera pasado" si el SL fuera
    # más ancho — sin depender de reconstruir el historial después con
    # una fuente externa. No afecta el resultado ya cerrado, es pura
    # observación adicional.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seguimiento_v55 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sim_id INTEGER NOT NULL,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            precio_entrada REAL,
            precio_cierre REAL,
            resultado_cierre_pct REAL,
            motivo_cierre TEXT,
            creado TEXT NOT NULL,
            terminado INTEGER DEFAULT 0,
            precio_1h REAL, resultado_1h_pct REAL,
            precio_2h REAL, resultado_2h_pct REAL,
            precio_4h REAL, resultado_4h_pct REAL,
            precio_6h REAL, resultado_6h_pct REAL,
            precio_8h REAL, resultado_8h_pct REAL,
            precio_12h REAL, resultado_12h_pct REAL
        )
    """)

    # 24/09 — Directiva V5.5 ("Estrategia Simplificada"): reemplaza a
    # V5.0 como la que opera con capital REAL (V5.0 pasa a modo sombra
    # exclusivo desde acá, junto con fix28/Simulación original/Directivas/
    # Combo, que ya estaban en sombra). Mismo pool de candidatos que V5.0
    # (mismos gates: filtro de universo, EMA4h, persistencia, funding,
    # ADX+RSI) — lo que cambia es el RANKING (puro, sin pendiente) y la
    # gestión de riesgo (SL/TP). Recopila SIEMPRE, sin importar la pausa.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS simulaciones_v55 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            score INTEGER,
            adx REAL,
            atr_pct REAL,
            precio_entrada REAL,
            pico_maximo_pct REAL DEFAULT 0,
            capital_asignado REAL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)

    # 16/09: capital_asignado para calcular el resultado PONDERADO (no
    # la suma simple) — misma fórmula documentada en v16: cada
    # operación aporta (resultado_pct/100 * capital_asignado) en USD,
    # el total se divide por el capital total de la cartera. Las
    # simulaciones usan el mismo 5% que usaría la real, para que la
    # comparación sea de manzanas con manzanas.
    TABLAS_CON_CAPITAL_ASIGNADO = (
        "simulaciones", "simulaciones_directivas", "simulaciones_combo",
        "simulaciones_fix28_fiel", "simulaciones_v5_fiel", "simulaciones_v55",
        "sombra_ranked_v55",
    )
    for tabla in TABLAS_CON_CAPITAL_ASIGNADO:
        try:
            cur.execute(f"ALTER TABLE {tabla} ADD COLUMN capital_asignado REAL")
        except Exception:
            pass  # ya existe

    _backfill_capital_asignado(cur)


def _backfill_capital_asignado(cur):
    """
    16/09 FIX, ampliado 26/09 — las simulaciones creadas cuando no había
    capital_diario del día quedaban con capital_asignado en NULL (antes)
    o en 0.0 (bug encontrado el 26/09: `_capital_asignado_estimado()`
    devolvía 0.0 en vez de None cuando faltaba el capital del día, y como
    0.0 no es NULL, este backfill nunca las corregía — quedaban pegadas
    en "neto real: +0.00%" para siempre aunque la suma simple fuera
    correcta). Se corrige el bug en `_capital_asignado_estimado()`
    (ahora deja NULL) y ACÁ se amplía la condición para reparar también
    las filas que ya quedaron en 0.0 por el bug viejo, apenas exista un
    capital_diario real para su fecha (o el más cercano anterior).
    """
    TABLAS_CON_CAPITAL_ASIGNADO = (
        "simulaciones", "simulaciones_directivas", "simulaciones_combo",
        "simulaciones_fix28_fiel", "simulaciones_v5_fiel", "simulaciones_v55",
        "sombra_ranked_v55",
    )
    for tabla in TABLAS_CON_CAPITAL_ASIGNADO:
        cur.execute(f"""
            UPDATE {tabla}
            SET capital_asignado = (
                SELECT ROUND(capital_diario.capital_dia * 0.05, 2)
                FROM capital_diario
                WHERE capital_diario.fecha = {tabla}.fecha
            )
            WHERE (capital_asignado IS NULL OR capital_asignado = 0)
              AND EXISTS (SELECT 1 FROM capital_diario WHERE capital_diario.fecha = {tabla}.fecha)
        """)
        # Respaldo: si no hay registro EXACTO de esa fecha (ej. el bot
        # estuvo caído justo a las 00:01), usa el capital_diario más
        # cercano ANTERIOR disponible — mejor estimación que dejar 0.
        cur.execute(f"""
            UPDATE {tabla}
            SET capital_asignado = (
                SELECT ROUND(capital_diario.capital_dia * 0.05, 2)
                FROM capital_diario
                WHERE capital_diario.fecha <= {tabla}.fecha
                ORDER BY capital_diario.fecha DESC
                LIMIT 1
            )
            WHERE (capital_asignado IS NULL OR capital_asignado = 0)
              AND EXISTS (SELECT 1 FROM capital_diario WHERE capital_diario.fecha <= {tabla}.fecha)
        """)


def backfill_capital_asignado():
    """Versión standalone (abre su propia conexión) para llamar bajo demanda desde un comando, sin esperar al próximo reinicio del bot."""
    conn = _conn()
    cur = conn.cursor()
    _backfill_capital_asignado(cur)
    conn.commit()
    conn.close()


# ── 26/09 — Directiva: registro del LOTE COMPLETO de candidatos de
# V5.5 en cada ciclo (ejecutados y descartados), capturado justo antes
# del recorte a los 2 mejores. Objetivo: poder medir después cantidad
# y calidad de las señales que no llegaron a simularse, para evaluar
# si conviene abrir más de 2 por ciclo.
def _crear_tabla_candidatos_v55_ciclo(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS candidatos_v55_ciclo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ciclo_ts TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            rsi_15m REAL,
            score REAL,
            posicion INTEGER NOT NULL,
            ejecutado INTEGER DEFAULT 0,
            precio REAL,
            creado TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_candidatos_v55_ciclo_fecha ON candidatos_v55_ciclo (fecha)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_candidatos_v55_ciclo_ciclo_ts ON candidatos_v55_ciclo (ciclo_ts)")

    # 26/09 — Columnas de seguimiento por checkpoints fijos (1/2/4/6/8/12hs),
    # SUPERADAS por sombra_ranked_v55 (simulación continua con la lógica de
    # salida real) más abajo — se dejan acá solo para no romper una base ya
    # migrada con este esquema, no se usan más activamente.
    columnas_seguimiento = [
        ("seguimiento_activo", "INTEGER DEFAULT 0"),
        ("terminado", "INTEGER DEFAULT 0"),
        ("precio_1h", "REAL"), ("resultado_1h_pct", "REAL"),
        ("precio_2h", "REAL"), ("resultado_2h_pct", "REAL"),
        ("precio_4h", "REAL"), ("resultado_4h_pct", "REAL"),
        ("precio_6h", "REAL"), ("resultado_6h_pct", "REAL"),
        ("precio_8h", "REAL"), ("resultado_8h_pct", "REAL"),
        ("precio_12h", "REAL"), ("resultado_12h_pct", "REAL"),
    ]
    for nombre, tipo in columnas_seguimiento:
        try:
            cur.execute(f"ALTER TABLE candidatos_v55_ciclo ADD COLUMN {nombre} {tipo}")
        except Exception:
            pass  # ya existe
    cur.execute("CREATE INDEX IF NOT EXISTS idx_candidatos_v55_ciclo_seguimiento ON candidatos_v55_ciclo (seguimiento_activo, terminado)")


TOP_SEGUIMIENTO_V55_CICLO = 10  # cuántos candidatos por ciclo (de arriba hacia abajo) reciben seguimiento de 12hs


def guardar_candidatos_v55_ciclo(candidatos_ordenados: list):
    """
    26/09 — Persistencia MASIVA (executemany, una sola vuelta a la DB)
    del ranking completo de un ciclo, ya ordenado de mejor a peor.
    `posicion` = 1-based según el orden recibido. `ejecutado` = 1 para
    los que quedaron en el top-2 (los que pasan a simularse/abrir),
    0 para el resto (descartados por el tope de 2 por ciclo).

    Nota: `ejecutado` refleja el corte del ranking, no si la simulación
    finalmente se creó — un candidato del top-2 puede igual no llegar a
    simularse si `hay_lugar_para_abrir_v55()` no tiene lugar (tope de 6
    simultáneas). Esa distinción no está contemplada acá todavía.
    """
    if not candidatos_ordenados:
        return
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    ciclo_ts = ahora.isoformat()
    fecha = ahora.strftime("%Y%m%d")
    hora = ahora.strftime("%H:%M")
    filas = [
        (
            ciclo_ts, fecha, hora,
            c.get("par"), c.get("direccion"), c.get("rsi_15m"), c.get("fuerza_score_v55"),
            i, 1 if i <= 2 else 0, c.get("precio"),
            1 if i <= TOP_SEGUIMIENTO_V55_CICLO else 0, ciclo_ts,
        )
        for i, c in enumerate(candidatos_ordenados, start=1)
    ]
    cur.executemany("""
        INSERT INTO candidatos_v55_ciclo
            (ciclo_ts, fecha, hora, par, direccion, rsi_15m, score, posicion, ejecutado, precio, seguimiento_activo, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, filas)
    conn.commit()
    conn.close()


# ── 26/09 — Directiva (v2): reemplaza el seguimiento por checkpoints
# fijos de arriba — Juanjo pidió que el informe diga de verdad "qué
# hubiera pasado", no una aproximación por precio a horas fijas. Para
# eso, los candidatos de posición 1..TOP_SOMBRA_RANKED_V55 de cada
# ciclo se simulan CONTINUAMENTE (cada 2seg, mismo hilo que
# simulaciones_v55) con la MISMA lógica de salida real
# (gestion_riesgo.evaluar_cierre_v55: SL -25% apalancado / trailing por
# pico) — no son 6 fotos de precio, es la posición completa abierta y
# cerrada con la lógica real. Esto permite backtestear en retrospectiva
# CUALQUIER combinación de "N aperturas por ciclo / M tope simultáneo"
# con datos reales de fidelidad completa, no una suposición.
TOP_SOMBRA_RANKED_V55 = 10  # hasta qué posición del ranking se simula continuamente


def _crear_tabla_sombra_ranked_v55(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sombra_ranked_v55 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ciclo_ts TEXT NOT NULL,
            fecha TEXT NOT NULL,
            hora TEXT NOT NULL,
            par TEXT NOT NULL,
            direccion TEXT NOT NULL,
            posicion INTEGER NOT NULL,
            score REAL,
            rsi_15m REAL,
            precio_entrada REAL,
            pico_maximo_pct REAL DEFAULT 0,
            capital_asignado REAL,
            cerrado INTEGER DEFAULT 0,
            resultado_pct REAL,
            motivo_cierre TEXT,
            fecha_cierre TEXT,
            hora_cierre TEXT,
            creado TEXT NOT NULL
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sombra_ranked_v55_par_cerrado ON sombra_ranked_v55 (par, cerrado)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sombra_ranked_v55_fecha ON sombra_ranked_v55 (fecha)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sombra_ranked_v55_posicion ON sombra_ranked_v55 (posicion)")


def par_tiene_sombra_ranked_v55_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sombra_ranked_v55 WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def abrir_sombra_ranked_v55_lote(candidatos_ordenados: list, top: int = TOP_SOMBRA_RANKED_V55):
    """
    Abre (si el par no tiene ya una abierta) una posición de sombra
    continua para cada candidato de posición 1..top del ciclo. Cada una
    se evalúa después con la MISMA evaluar_cierre_v55 que la real y que
    simulaciones_v55, en el hilo de 2seg (ver sombra_ranked_v55_abiertas).
    """
    if not candidatos_ordenados:
        return
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    ciclo_ts = ahora.isoformat()
    fecha = ahora.strftime("%Y%m%d")
    hora = ahora.strftime("%H:%M")
    capital_asignado = _capital_asignado_estimado()
    filas = []
    for i, c in enumerate(candidatos_ordenados[:top], start=1):
        par = c.get("par")
        if par_tiene_sombra_ranked_v55_abierta(par):
            continue
        filas.append((
            ciclo_ts, fecha, hora, par, c.get("direccion"), i,
            c.get("fuerza_score_v55"), c.get("rsi_15m"), c.get("precio"), capital_asignado, ciclo_ts,
        ))
    if filas:
        cur.executemany("""
            INSERT INTO sombra_ranked_v55
                (ciclo_ts, fecha, hora, par, direccion, posicion, score, rsi_15m, precio_entrada, capital_asignado, creado)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, filas)
        conn.commit()
    conn.close()


def sombra_ranked_v55_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sombra_ranked_v55 WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_pico_sombra_ranked_v55(id_: int, pico_nuevo: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE sombra_ranked_v55 SET pico_maximo_pct = ? WHERE id = ?", (pico_nuevo, id_))
    conn.commit()
    conn.close()


def cerrar_sombra_ranked_v55(id_: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE sombra_ranked_v55 SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), id_))
    conn.commit()
    conn.close()


def informe_candidatos_v55(desde_fecha: str = None, hasta_fecha: str = None) -> dict:
    """
    26/09 — Cantidad y calidad REAL de candidatos por posición del
    ranking, usando los cierres YA SIMULADOS de sombra_ranked_v55 (con
    la lógica de salida real, no una aproximación por precio a horas
    fijas). Cuenta también cuántos candidatos calificaron en total por
    ciclo (tabla candidatos_v55_ciclo, todas las posiciones).
    """
    conn = _conn()
    cur = conn.cursor()

    query_c = "SELECT ciclo_ts, posicion FROM candidatos_v55_ciclo WHERE 1=1"
    params_c = []
    if desde_fecha:
        query_c += " AND fecha >= ?"
        params_c.append(desde_fecha)
    if hasta_fecha:
        query_c += " AND fecha <= ?"
        params_c.append(hasta_fecha)
    cur.execute(query_c, tuple(params_c))
    filas_candidatos = cur.fetchall()

    query_s = "SELECT * FROM sombra_ranked_v55 WHERE cerrado = 1 AND resultado_pct IS NOT NULL"
    params_s = []
    if desde_fecha:
        query_s += " AND fecha >= ?"
        params_s.append(desde_fecha)
    if hasta_fecha:
        query_s += " AND fecha <= ?"
        params_s.append(hasta_fecha)
    cur.execute(query_s, tuple(params_s))
    filas_sombra = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not filas_candidatos:
        return {"n_ciclos": 0}

    n_ciclos = len(set(r[0] for r in filas_candidatos))
    total = len(filas_candidatos)

    buckets = [
        ("pos_1_2 (ejecutadas)", lambda p: p <= 2),
        ("pos_3", lambda p: p == 3),
        ("pos_4", lambda p: p == 4),
        ("pos_5_10", lambda p: 5 <= p <= 10),
    ]

    detalle = {}
    for nombre, cond in buckets:
        cerradas = [f for f in filas_sombra if cond(f["posicion"])]
        if cerradas:
            ganadoras = [f for f in cerradas if f["resultado_pct"] > 0]
            detalle[nombre] = {
                "n_cerradas": len(cerradas),
                "win_rate_pct": round(len(ganadoras) / len(cerradas) * 100, 1),
                "resultado_prom_pct": round(sum(f["resultado_pct"] for f in cerradas) / len(cerradas), 2),
            }
        else:
            detalle[nombre] = {"n_cerradas": 0}

    return {
        "n_ciclos": n_ciclos,
        "total_candidatos": total,
        "promedio_por_ciclo": round(total / n_ciclos, 1) if n_ciclos else None,
        "por_posicion": detalle,
    }


def informe_combo_v55(n: int, m: int, desde_fecha: str = None, hasta_fecha: str = None) -> dict:
    """
    26/09 — Backtest retroactivo real: qué hubiera dado abrir hasta `n`
    candidatos por ciclo (por orden de ranking) con un tope de `m`
    posiciones simultáneas, reproduciendo cronológicamente los cierres
    YA SIMULADOS de sombra_ranked_v55 (fidelidad completa: SL -25%
    apalancado / trailing, igual que la real).

    Algoritmo: recorre los candidatos de posición <= n en orden de
    apertura; en cada uno, libera del "cupo" ocupado las posiciones que
    ya habrían cerrado para esa fecha/hora; si hay lugar (menos de m
    ocupadas Y el par no tiene ya una ocupada) lo acepta, si no lo
    cuenta como bloqueado por el tope — mismo criterio que
    hay_lugar_para_abrir_v55(), aplicado en retrospectiva.
    """
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM sombra_ranked_v55 WHERE posicion <= ? AND cerrado = 1 AND resultado_pct IS NOT NULL"
    params = [n]
    if desde_fecha:
        query += " AND fecha >= ?"
        params.append(desde_fecha)
    if hasta_fecha:
        query += " AND fecha <= ?"
        params.append(hasta_fecha)
    query += " ORDER BY ciclo_ts ASC"
    cur.execute(query, tuple(params))
    filas = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not filas:
        return {"n_candidatos": 0}

    ocupadas = []  # [(par, cierre_dt_o_None)]
    aceptadas = []
    bloqueadas_tope = 0

    for f in filas:
        try:
            apertura_dt = datetime.fromisoformat(f["ciclo_ts"])
        except Exception:
            continue
        cierre_dt = None
        if f.get("fecha_cierre") and f.get("hora_cierre"):
            try:
                cierre_dt = datetime.strptime(f"{f['fecha_cierre']} {f['hora_cierre']}", "%Y%m%d %H:%M").replace(tzinfo=TZ_ARG)
            except Exception:
                cierre_dt = None

        ocupadas = [(p, c) for (p, c) in ocupadas if c is None or c > apertura_dt]
        par_ocupado = any(p == f["par"] for p, c in ocupadas)
        if par_ocupado or len(ocupadas) >= m:
            bloqueadas_tope += 1
            continue
        ocupadas.append((f["par"], cierre_dt))
        aceptadas.append(f)

    if not aceptadas:
        return {"n_candidatos": len(filas), "n_aceptadas": 0, "n_bloqueadas_tope": bloqueadas_tope}

    cap_hoy = obtener_capital_diario()
    capital_total = cap_hoy["capital_dia"] if cap_hoy else None
    if capital_total is None:
        conn2 = _conn()
        cur2 = conn2.cursor()
        cur2.execute("SELECT capital_dia FROM capital_diario ORDER BY fecha DESC LIMIT 1")
        row2 = cur2.fetchone()
        conn2.close()
        capital_total = row2[0] if row2 else None

    ganadoras = [f for f in aceptadas if f["resultado_pct"] > 0]
    ganancia_usd = sum((f["resultado_pct"] / 100) * (f.get("capital_asignado") or 0) for f in aceptadas)
    neto_ponderado_pct = round((ganancia_usd / capital_total) * 100, 2) if capital_total else None

    return {
        "n_candidatos": len(filas),
        "n_aceptadas": len(aceptadas),
        "n_bloqueadas_tope": bloqueadas_tope,
        "n_ganadoras": len(ganadoras),
        "n_perdedoras": len(aceptadas) - len(ganadoras),
        "win_rate_pct": round(len(ganadoras) / len(aceptadas) * 100, 1),
        "resultado_neto_pct": round(sum(f["resultado_pct"] for f in aceptadas), 2),
        "ganancia_usd": round(ganancia_usd, 2),
        "neto_ponderado_pct": neto_ponderado_pct,
    }


def ultimo_ciclo_v55_candidatos() -> list:
    """26/09 — El ranking completo del ciclo más reciente ya persistido, ordenado por posición. Para verificar el registro tras el deploy."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(ciclo_ts) FROM candidatos_v55_ciclo")
    row = cur.fetchone()
    ciclo_ts = row[0] if row else None
    if not ciclo_ts:
        conn.close()
        return []
    cur.execute("SELECT * FROM candidatos_v55_ciclo WHERE ciclo_ts = ? ORDER BY posicion ASC", (ciclo_ts,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _capital_asignado_estimado():
    """
    5% del capital de hoy (mismo % que usaría una posición real) — para
    que las simulaciones sean comparables con capital real.

    26/09 FIX: devolvía 0.0 cuando todavía no había capital_diario del
    día (recálculo pospuesto/fallido) — como 0.0 no es NULL, el backfill
    de más arriba nunca corregía esas filas después, y quedaban con
    "neto real: +0.00%" para siempre aunque la suma simple mostrara el
    resultado correcto (bug detectado por Juanjo el 26/09). Ahora
    devuelve None (queda NULL en la fila), así el backfill SÍ la agarra
    apenas exista un capital_diario real para esa fecha.
    """
    cap = obtener_capital_diario()
    if not cap:
        return None
    return round(cap["capital_dia"] * 0.05, 2)


def crear_simulacion(par, direccion, score, adx, atr_pct, precio_entrada) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    capital_asignado = _capital_asignado_estimado()
    cur.execute("""
        INSERT INTO simulaciones (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, fecha, hora, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def par_tiene_simulacion_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def simulaciones_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulaciones WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_pico_simulacion(sim_id: int, pico_nuevo: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE simulaciones SET pico_maximo_pct = ? WHERE id = ?", (pico_nuevo, sim_id))
    conn.commit()
    conn.close()


def cerrar_simulacion(sim_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def resumen_simulaciones(desde_fecha: str = None) -> dict:
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM simulaciones WHERE cerrado = 1 AND resultado_pct IS NOT NULL"
    params = ()
    if desde_fecha:
        query += " AND fecha_cierre >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    cerradas = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not cerradas:
        return {"n_cerradas": 0}
    ganadoras = [f for f in cerradas if f["resultado_pct"] > 0]
    por_direccion = {}
    for d in ["LARGO", "CORTO"]:
        sub = [f for f in cerradas if f.get("direccion") == d]
        if sub:
            g = [f for f in sub if f["resultado_pct"] > 0]
            por_direccion[d] = {"n": len(sub), "win_rate": round(len(g) / len(sub) * 100, 1), "neto": round(sum(f["resultado_pct"] for f in sub), 2)}
    return {
        "n_cerradas": len(cerradas), "n_ganadoras": len(ganadoras), "n_perdedoras": len(cerradas) - len(ganadoras),
        "win_rate_pct": round(len(ganadoras) / len(cerradas) * 100, 1),
        "resultado_neto_pct": round(sum(f["resultado_pct"] for f in cerradas), 2),
        "por_direccion": por_direccion,
    }


# ── 14/09: mismas funciones, para la simulación de Directivas ──
def par_tiene_simulacion_directivas_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones_directivas WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def crear_simulacion_directivas(par, direccion, score, adx, atr_pct, precio_entrada) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    capital_asignado = _capital_asignado_estimado()
    cur.execute("""
        INSERT INTO simulaciones_directivas (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, fecha, hora, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def par_tiene_simulacion_v5_fiel_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones_v5_fiel WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def crear_simulacion_v5_fiel(par, direccion, score, adx, atr_pct, precio_entrada) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    capital_asignado = _capital_asignado_estimado()
    cur.execute("""
        INSERT INTO simulaciones_v5_fiel (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, fecha, hora, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def simulaciones_v5_fiel_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulaciones_v5_fiel WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_pico_simulacion_v5_fiel(sim_id: int, pico_nuevo: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE simulaciones_v5_fiel SET pico_maximo_pct = ? WHERE id = ?", (pico_nuevo, sim_id))
    conn.commit()
    conn.close()


def cerrar_simulacion_v5_fiel(sim_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones_v5_fiel SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


# ── 24/09 — Directiva V5.5 ("Estrategia Simplificada"): AHORA LA REAL ──
def par_tiene_simulacion_v55_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones_v55 WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def crear_simulacion_v55(par, direccion, score, adx, atr_pct, precio_entrada) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    capital_asignado = _capital_asignado_estimado()
    cur.execute("""
        INSERT INTO simulaciones_v55 (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, fecha, hora, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def simulaciones_v55_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulaciones_v55 WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_pico_simulacion_v55(sim_id: int, pico_nuevo: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE simulaciones_v55 SET pico_maximo_pct = ? WHERE id = ?", (pico_nuevo, sim_id))
    conn.commit()
    conn.close()


def cerrar_simulacion_v55(sim_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones_v55 SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


# ── 25/09 — Seguimiento post-cierre de V5.5 fiel (12hs, checkpoints fijos) ──
CHECKPOINTS_SEGUIMIENTO_V55 = (1, 2, 4, 6, 8, 12)  # horas desde el cierre


def crear_seguimiento_v55(sim_id: int, par: str, direccion: str, precio_entrada: float, precio_cierre: float,
                           resultado_cierre_pct: float, motivo_cierre: str) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO seguimiento_v55 (sim_id, par, direccion, precio_entrada, precio_cierre, resultado_cierre_pct, motivo_cierre, creado)
        VALUES (?,?,?,?,?,?,?,?)
    """, (sim_id, par, direccion, precio_entrada, precio_cierre, resultado_cierre_pct, motivo_cierre, ahora.isoformat()))
    conn.commit()
    seg_id = cur.lastrowid
    conn.close()
    return seg_id


def seguimientos_v55_pendientes() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM seguimiento_v55 WHERE terminado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def guardar_checkpoint_seguimiento_v55(seg_id: int, horas: int, precio: float, resultado_pct: float):
    """Guarda el checkpoint de N horas (1/2/4/6/8/12) — columnas fijas, una por checkpoint."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE seguimiento_v55 SET precio_{horas}h = ?, resultado_{horas}h_pct = ? WHERE id = ?",
                (precio, resultado_pct, seg_id))
    conn.commit()
    conn.close()


def marcar_seguimiento_v55_terminado(seg_id: int):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE seguimiento_v55 SET terminado = 1 WHERE id = ?", (seg_id,))
    conn.commit()
    conn.close()


def seguimientos_v55_recientes(limite: int = 10) -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM seguimiento_v55 ORDER BY id DESC LIMIT ?", (limite,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def par_tiene_simulacion_combo_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones_combo WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def crear_simulacion_combo(par, direccion, score, adx, atr_pct, precio_entrada) -> int:
    """16/09 — 4ta simulación: entrada de Directivas (ADX≤30 LARGO + bloqueo sobrecompra) + salida de la simulación original (SL -7.5% + trailing 3 tramos por ATR)."""
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    capital_asignado = _capital_asignado_estimado()
    cur.execute("""
        INSERT INTO simulaciones_combo (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, fecha, hora, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, score, adx, atr_pct, precio_entrada, capital_asignado, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def simulaciones_combo_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulaciones_combo WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_pico_simulacion_combo(sim_id: int, pico_nuevo: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE simulaciones_combo SET pico_maximo_pct = ? WHERE id = ?", (pico_nuevo, sim_id))
    conn.commit()
    conn.close()


def cerrar_simulacion_combo(sim_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones_combo SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def par_tiene_simulacion_fix28_fiel_abierta(par: str) -> bool:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones_fix28_fiel WHERE cerrado = 0 AND par = ?", (par,))
    n = cur.fetchone()[0]
    conn.close()
    return n > 0


def crear_simulacion_fix28_fiel(par, direccion, score, adx, atr_pct, precio_entrada, rango_bajo, rango_alto) -> int:
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    capital_asignado = _capital_asignado_estimado()
    cur.execute("""
        INSERT INTO simulaciones_fix28_fiel (par, direccion, score, adx, atr_pct, precio_entrada, rango_bajo, rango_alto, capital_asignado, fecha, hora, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, score, adx, atr_pct, precio_entrada, rango_bajo, rango_alto, capital_asignado,
          ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), ahora.isoformat()))
    conn.commit()
    sim_id = cur.lastrowid
    conn.close()
    return sim_id


def simulaciones_fix28_fiel_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulaciones_fix28_fiel WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_simulacion_fix28_fiel(sim_id: int, pico_nuevo: float, fuera_rango_desde_nuevo):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE simulaciones_fix28_fiel SET pico_maximo_pct = ?, fuera_rango_desde = ? WHERE id = ?",
                (pico_nuevo, fuera_rango_desde_nuevo, sim_id))
    conn.commit()
    conn.close()


def cerrar_simulacion_fix28_fiel(sim_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones_fix28_fiel SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def simulaciones_directivas_abiertas() -> list:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM simulaciones_directivas WHERE cerrado = 0")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def actualizar_pico_simulacion_directivas(sim_id: int, pico_nuevo: float):
    conn = _conn()
    cur = conn.cursor()
    cur.execute("UPDATE simulaciones_directivas SET pico_maximo_pct = ? WHERE id = ?", (pico_nuevo, sim_id))
    conn.commit()
    conn.close()


def cerrar_simulacion_directivas(sim_id: int, resultado_pct: float, motivo: str):
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        UPDATE simulaciones_directivas SET cerrado = 1, resultado_pct = ?, motivo_cierre = ?, fecha_cierre = ?, hora_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), sim_id))
    conn.commit()
    conn.close()


def resumen_simulaciones_directivas(desde_fecha: str = None) -> dict:
    conn = _conn()
    cur = conn.cursor()
    query = "SELECT * FROM simulaciones_directivas WHERE cerrado = 1 AND resultado_pct IS NOT NULL"
    params = ()
    if desde_fecha:
        query += " AND fecha_cierre >= ?"
        params = (desde_fecha,)
    cur.execute(query, params)
    cerradas = [dict(r) for r in cur.fetchall()]
    conn.close()
    if not cerradas:
        return {"n_cerradas": 0}
    ganadoras = [f for f in cerradas if f["resultado_pct"] > 0]
    return {
        "n_cerradas": len(cerradas), "n_ganadoras": len(ganadoras), "n_perdedoras": len(cerradas) - len(ganadoras),
        "win_rate_pct": round(len(ganadoras) / len(cerradas) * 100, 1),
        "resultado_neto_pct": round(sum(f["resultado_pct"] for f in cerradas), 2),
    }


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
                       score: int, score_momentum: int, califico: bool,
                       atr_pct: float = None, rsi: float = None, volumen_ratio: float = None,
                       estrategia: str = "fix28"):
    """
    11/09 — Se agregaron atr_pct, rsi, volumen_ratio (opcionales, con
    default None para no romper llamados viejos) — ANTES solo se
    guardaban para candidatos que llegaban a abrir de verdad (tabla
    senales), no para los rechazados. Esto impidió backtestear el
    filtro de vela+RSI (fix25) con datos propios cuando hizo falta.
    Guardando esto para TODOS los candidatos evaluados, el próximo
    backtest va a poder probar variantes de ATR/RSI/volumen sin
    depender de que abran posiciones reales primero.

    19/09 FIX: se agregó "estrategia" (fix28/v5) — antes /gates
    mezclaba chequeos de las 2 sin poder distinguir cuál generó cada
    fila, imposible de diagnosticar bien. Default "fix28" para no
    romper el único otro llamado que no lo pasa todavía.
    """
    conn = _conn()
    cur = conn.cursor()
    ahora = datetime.now(TZ_ARG)
    cur.execute("""
        INSERT INTO gates_log
            (par, direccion, fecha, hora, adx, adx_umbral_usado, paso_adx, paso_ema4h,
             paso_funding, score, score_momentum, califico, atr_pct, rsi, volumen_ratio, estrategia, creado)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (par, direccion, ahora.strftime("%Y%m%d"), ahora.strftime("%H:%M"), adx, adx_umbral_usado,
          int(paso_adx), int(paso_ema4h), int(paso_funding), score, score_momentum, int(califico),
          atr_pct, rsi, volumen_ratio, estrategia, ahora.isoformat()))
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


def contar_simulaciones_v55_abiertas() -> int:
    """
    24/09 — Directiva V5.5: equivalente de contar_posiciones_abiertas()
    pero para la tabla de simulación, para que el tope de 6 simultáneas
    quede reflejado en la sombra también cuando el bot está pausado (o
    simplemente no tiene lugar en la tabla real todavía).
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM simulaciones_v55 WHERE cerrado = 0")
    n = cur.fetchone()[0]
    conn.close()
    return n


def contar_aperturas_v55_ultimos_minutos(minutos: int = 15) -> int:
    """Equivalente de contar_aperturas_ultimos_minutos() para simulaciones_v55 (tope de 2 por ciclo de 15min)."""
    conn = _conn()
    cur = conn.cursor()
    limite = (datetime.now(TZ_ARG) - timedelta(minutes=minutos)).isoformat()
    cur.execute("""
        SELECT COUNT(*) FROM simulaciones_v55 WHERE creado >= ?
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
                            tiempo_real_min = ?, hora_cierre = ?, fecha_cierre = ?
        WHERE id = ?
    """, (resultado_pct, motivo, tiempo_real_min, datetime.now(TZ_ARG).strftime("%H:%M"),
          datetime.now(TZ_ARG).strftime("%Y%m%d"), senal_id))
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
def resumen_ponderado(tabla: str, desde_fecha: str = None, hasta_fecha: str = None) -> dict:
    """
    16/09 — Fórmula CORRECTA documentada en v16 (encontrada en
    conocimiento del proyecto): pondera cada operación por el capital
    REAL que usó, no suma los % a lo bruto. La suma simple infla el
    resultado ~20x en Bot Cripto (cada operación usa solo 5% del
    capital, pero sumaba su % completo como si hubiera usado el 100%).
    Sirve para las 4 tablas: senales (real) y las 3 simulaciones —
    todas tienen capital_asignado calculado con el mismo criterio (5%
    del capital de ese día), así que son comparables entre sí.
    """
    conn = _conn()
    cur = conn.cursor()
    campo_bu = "bu_order_id IS NOT NULL AND " if tabla == "senales" else ""
    query = f"SELECT resultado_pct, capital_asignado, fecha FROM {tabla} WHERE {campo_bu}cerrado = 1 AND resultado_pct IS NOT NULL"
    params = []
    if desde_fecha:
        query += " AND fecha_cierre >= ?"
        params.append(desde_fecha)
    if hasta_fecha:
        query += " AND fecha_cierre <= ?"
        params.append(hasta_fecha)
    cur.execute(query, tuple(params))
    filas = cur.fetchall()

    # 26/09 FIX: antes, una fila sin capital_asignado (recálculo diario
    # pospuesto o fallido ese día) se trataba en silencio como si hubiera
    # usado $0 de capital — el "neto real" daba +0.00% en vez de reflejar
    # el resultado, indistinguible de un resultado genuino de 0 (bug
    # detectado por Juanjo el 26/09). Ahora, si falta, se resuelve al
    # vuelo con el capital_dia de esa MISMA fecha (o el más cercano
    # anterior) en vez de depender solo del backfill guardado en la fila.
    cur.execute("SELECT fecha, capital_dia FROM capital_diario ORDER BY fecha ASC")
    capital_por_fecha = {f: c for f, c in cur.fetchall()}
    fechas_con_capital = sorted(capital_por_fecha.keys())
    conn.close()

    if not filas:
        return {"n_cerradas": 0}

    cap_hoy = obtener_capital_diario()
    capital_total = cap_hoy["capital_dia"] if cap_hoy else None
    if capital_total is None:
        # 17/09 FIX: si el recálculo de hoy no corrió todavía (ej.
        # siempre hubo una posición abierta justo a las 00:01, que
        # pospone el recálculo indefinidamente mientras el bot está
        # activo) — usar el capital_dia MÁS RECIENTE disponible, en
        # vez de mostrar "s/d" cada vez que esto pase.
        capital_total = capital_por_fecha[fechas_con_capital[-1]] if fechas_con_capital else None

    def _capital_asignado_para_fecha(fecha):
        if fecha in capital_por_fecha:
            return round(capital_por_fecha[fecha] * 0.05, 2)
        anteriores = [f for f in fechas_con_capital if f <= fecha]
        if anteriores:
            return round(capital_por_fecha[anteriores[-1]] * 0.05, 2)
        return None

    ganancia_usd = 0.0
    n_ganadoras = 0
    resultados_pct = []
    n_sin_capital = 0
    for resultado_pct, capital_asignado, fecha in filas:
        resultados_pct.append(resultado_pct)
        if resultado_pct > 0:
            n_ganadoras += 1
        cap_op = capital_asignado
        if not cap_op:
            cap_op = _capital_asignado_para_fecha(fecha)
            if not cap_op:
                n_sin_capital += 1
                cap_op = 0
        ganancia_usd += (resultado_pct / 100) * cap_op

    neto_ponderado_pct = round((ganancia_usd / capital_total) * 100, 2) if capital_total else None

    resultado = {
        "n_cerradas": len(filas),
        "n_ganadoras": n_ganadoras,
        "n_perdedoras": len(filas) - n_ganadoras,
        "win_rate_pct": round(n_ganadoras / len(filas) * 100, 1),
        "resultado_neto_pct": round(sum(resultados_pct), 2),  # suma simple, se mantiene como referencia
        "ganancia_usd": round(ganancia_usd, 2),
        "neto_ponderado_pct": neto_ponderado_pct,  # el número correcto
    }
    if n_sin_capital:
        # No hay NINGÚN capital_diario (ni de esa fecha ni anterior) para
        # estas filas — pasa solo si el bot nunca tuvo un recálculo
        # exitoso todavía. Se avisa en vez de contarlas silenciosamente
        # como $0 de capital.
        resultado["n_sin_capital"] = n_sin_capital
    return resultado


def resumen_completo(desde_fecha: str = None, hasta_fecha: str = None, por_cierre: bool = False) -> dict:
    """
    07/09 — Informe completo para análisis, en un solo comando. Incluye
    desglose por motivo de cierre y score ganadoras/perdedoras.

    10/09 — 2 mejoras pedidas:
    - hasta_fecha: permite acotar un rango exacto (antes solo "desde tal
      fecha hasta hoy"), ej. desde_fecha=hasta_fecha para un solo día.
    - por_cierre: filtra por FECHA DE CIERRE en vez de fecha de apertura
      (antes solo existía el filtro por apertura — una operación abierta
      ayer y cerrada hoy no aparecía en el informe de "hoy").
    """
    conn = _conn()
    cur = conn.cursor()
    campo_fecha = "fecha_cierre" if por_cierre else "fecha"
    query = "SELECT * FROM senales WHERE cerrado = 1 AND resultado_pct IS NOT NULL AND bu_order_id IS NOT NULL"
    params = []
    if desde_fecha:
        query += f" AND {campo_fecha} >= ?"
        params.append(desde_fecha)
    if hasta_fecha:
        query += f" AND {campo_fecha} <= ?"
        params.append(hasta_fecha)
    cur.execute(query, tuple(params))
    cerradas = [dict(r) for r in cur.fetchall()]

    # Candidatos evaluados en el período (gates_log) vs. los que realmente abrieron
    query_gates = "SELECT COUNT(*) FROM gates_log"
    query_calif = "SELECT COUNT(*) FROM gates_log WHERE califico = 1"
    params_gates = ()
    if desde_fecha:
        query_gates += " WHERE fecha >= ?"
        query_calif += " AND fecha >= ?"
        params_gates = (desde_fecha,)
    cur.execute(query_gates, params_gates)
    total_evaluados = cur.fetchone()[0]
    cur.execute(query_calif, params_gates)
    total_califico = cur.fetchone()[0]
    conn.close()

    if not cerradas:
        return {"n_cerradas": 0, "total_evaluados": total_evaluados, "total_califico": total_califico}

    ganadoras = [r for r in cerradas if r["resultado_pct"] > 0]
    perdedoras = [r for r in cerradas if r["resultado_pct"] <= 0]
    resultados = [r["resultado_pct"] for r in cerradas]

    def _prom(lst, campo="resultado_pct"):
        vals = [r[campo] for r in lst if r.get(campo) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    por_motivo = {}
    for r in cerradas:
        m = r.get("motivo_cierre") or "desconocido"
        por_motivo.setdefault(m, []).append(r["resultado_pct"])
    por_motivo_resumen = {m: {"n": len(v), "prom": round(sum(v) / len(v), 2)} for m, v in por_motivo.items()}

    return {
        "n_cerradas": len(cerradas),
        "n_ganadoras": len(ganadoras),
        "n_perdedoras": len(perdedoras),
        "win_rate_pct": round(len(ganadoras) / len(cerradas) * 100, 1),
        "ganancia_prom_pct": _prom(ganadoras),
        "perdida_prom_pct": _prom(perdedoras),
        "resultado_neto_pct": round(sum(resultados), 2),
        "mejor_pct": round(max(resultados), 2),
        "peor_pct": round(min(resultados), 2),
        "por_motivo": por_motivo_resumen,
        "score_prom_ganadoras": _prom(ganadoras, "score"),
        "score_prom_perdedoras": _prom(perdedoras, "score"),
        "total_evaluados": total_evaluados,
        "total_califico": total_califico,
    }


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
