# ============================================================================
# db.py — capa de base de datos COCHI CS
#
# Producción : Postgres en Supabase (SUPABASE_DB_URL) — datos persistentes,
#              sobreviven a redeploys y reinicios de Render.
# Local      : SQLite keys.db (igual que antes) cuando no hay SUPABASE_DB_URL.
#
# La API para app.py es IDENTICA en ambos motores:
#   q(sql, params)              -> lista de dicts
#   q(sql, params, one=True)    -> dict | None
#   q(sql, params, commit=True) -> INSERT/UPDATE/DELETE
#
# Si SUPABASE_DB_URL esta definida pero falla la conexion, el server MUERE con
# un mensaje claro (Render lo reinicia y queda visible en el log). Nunca cae
# en silencio a SQLite: ahi los datos se perderian en el proximo redeploy.
# ============================================================================
import os
import threading

_IS_PG = False
_url = (os.environ.get("SUPABASE_DB_URL") or "").strip()

if _url:
    from urllib.parse import unquote, urlparse

    u = urlparse(_url if "://" in _url else "postgres://" + _url)

    try:
        import psycopg2

        _PG = psycopg2.connect(
            host=u.hostname,
            port=u.port or 5432,
            dbname=(u.path or "/postgres").lstrip("/") or "postgres",
            user=unquote(u.username or "postgres"),
            password=unquote(u.password or ""),   # acepta password URL-encodeado o plano
            sslmode="require",          # obligatorio para Supabase
            connect_timeout=10,
        )
        _PG.autocommit = False
        _LOCK = threading.Lock()        # 1 conexion compartida, acceso serializado

        with _PG.cursor() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS licencias (
                key      TEXT PRIMARY KEY,
                tipo     TEXT NOT NULL,
                duracion INTEGER,
                techo    TEXT,
                vence    TEXT,
                hwid     TEXT,
                activado TEXT,
                cliente  TEXT,
                revocada INTEGER DEFAULT 0,
                creada   BIGINT)""")
        _PG.commit()
        _IS_PG = True
        print(f"[db] Supabase/Postgres conectado: {u.hostname}")

    except Exception as e:
        raise SystemExit(
            f"[db] FATAL: SUPABASE_DB_URL definida pero no se pudo conectar: {e}\n"
            "     Revisa la URI (password URL-encoded) o borra la variable para "
            "usar SQLite local.")

    def q(sql: str, params=(), one=False, commit=False, return_sql=None):
        sql = sql.replace("?", "%s")   # SQLite usa ?, Postgres %s
        with _LOCK:
            with _PG.cursor() as c:
                c.execute(sql, params)
                if commit:
                    _PG.commit()
                if return_sql:
                    c.execute(return_sql, params)
                if c.description is None:      # INSERT/UPDATE/DELETE: sin filas
                    rows = []
                else:
                    cols = [d[0] for d in c.description]
                    rows = [dict(zip(cols, r)) for r in c.fetchall()]
        return (rows[0] if rows else None) if one else rows

else:
    # ------------------------------------------------------------ SQLite local
    import sqlite3

    HERE = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(HERE, "keys.db")

    # esquema (una sola vez al importar)
    _c = sqlite3.connect(DB_PATH)
    _c.execute("""CREATE TABLE IF NOT EXISTS licencias (
        key      TEXT PRIMARY KEY,
        tipo     TEXT NOT NULL,
        duracion INTEGER,
        techo    TEXT,
        vence    TEXT,
        hwid     TEXT,
        activado TEXT,
        cliente  TEXT,
        revocada INTEGER DEFAULT 0,
        creada   INTEGER)""")
    _c.commit()
    _c.close()
    print("[db] SQLite local:", DB_PATH)

    def q(sql: str, params=(), one=False, commit=False, return_sql=None):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(sql, params)
            if commit:
                conn.commit()
            rows = [dict(r) for r in cur.fetchall()]
            if return_sql:
                rows = [dict(r) for r in conn.execute(return_sql, params).fetchall()]
            return (rows[0] if rows else None) if one else rows
        finally:
            conn.close()


if __name__ == "__main__":
    # prueba rapida:  python db.py
    _t = "COCHI-TEST-0001"
    q("INSERT INTO licencias (key,tipo,cliente,creada) VALUES (?,'hwid','_test',0)",
      (_t,), commit=True)
    print("insert:", q("SELECT key, cliente FROM licencias WHERE key=?", (_t,), one=True))
    q("DELETE FROM licencias WHERE key=?", (_t,), commit=True)
    print("delete ok  |  motor:", "Postgres/Supabase" if _IS_PG else "SQLite local")
