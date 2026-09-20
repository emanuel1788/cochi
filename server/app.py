#!/usr/bin/env python3
# ============================================================================
# app.py — COCHI CS License Server (Flask + Supabase/Postgres | SQLite local)
#
# Que hace:
#   - Guarda licencias via db.py (Supabase/Postgres en produccion, SQLite
#     keys.db en local): key, tipo, duracion, techo,
#     hwid ligado, fecha de activacion, revocada si/no
#   - POST /activate : el launcher manda {key, hwid}; registra el HWID la
#     primera vez, guarda la fecha de activacion, y devuelve el cochi.lic
#     FIRMADO (Ed25519, la misma clave que ya usas) listo para guardar
#   - POST /check    : revalidacion en caliente (key + hwid) -> {"valid":true}
#   - POST /trial    : trial self-service {hwid} -> 3 dias, 1 por HWID,
#     ligado a ese HWID (no compartible)
#   - POST /admin    : operaciones del dueno (crear/revocar/listar),
#     protegidas con ADMIN_TOKEN
#
# Despliegue: Render/Railway free tier (ver README del server).
# Local:       python app.py  ->  http://127.0.0.1:5000
# ============================================================================
import base64
import json
import os
import secrets
import time
from datetime import date, timedelta

from flask import Flask, jsonify, request

from db import q  # capa de datos: Supabase/Postgres (prod) o SQLite (local)

try:
    import nacl.signing
except ImportError:
    raise SystemExit("Falta pynacl:  pip install pynacl flask gunicorn")

HERE = os.path.dirname(os.path.abspath(__file__))
PRIVATE_KEY_PATH = os.path.join(HERE, "private.key")

# La clave privada que firma las licencias: la MISMA que licensing/private.key
# (asi el cheat publicado verifica los .lic del server sin recompilar nada).
# Dos formas de darle la clave al server:
#   - LICENSE_SEED (env var, hex de 64 chars) -> recomendado en Render/Railway
#   - private.key (archivo junto a app.py)    -> para correr local
seed_hex = os.environ.get("LICENSE_SEED", "").strip()
if seed_hex:
    SEED = bytes.fromhex(seed_hex)
elif os.path.isfile(PRIVATE_KEY_PATH):
    SEED = open(PRIVATE_KEY_PATH, "rb").read()
else:
    raise SystemExit(
        "Sin clave de firma: define LICENSE_SEED (hex de private.key) o copia\n"
        "private.key junto a app.py. NUNCA subas la clave a un repo publico.")
SIGNER = nacl.signing.SigningKey(SEED)

ADMIN_TOKEN = os.environ.get("COCHI_ADMIN_TOKEN", "cambia-esto-en-produccion")
TRIAL_DIAS = 3
TRIAL_GRACIA = 2

app = Flask(__name__)


def new_key() -> str:
    raw = secrets.token_hex(4).upper()
    return "COCHI-" + "-".join(raw[i:i + 4] for i in range(0, 8, 4))


def sign_lic(payload: dict) -> str:
    """payload -> archivo .lic de 2 lineas b64 (mismo formato que license_tool)."""
    msg = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = SIGNER.sign(msg).signature
    return base64.b64encode(msg).decode() + "\n" + base64.b64encode(sig).decode() + "\n"


def find(key: str):
    return q("SELECT * FROM licencias WHERE key=?", (key,), one=True)


def hwid_valido(h: str) -> bool:
    return isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdefABCDEF" for c in h)


# ---------------------------------------------------------------- cliente API
@app.post("/activate")
def activate():
    """El launcher manda {key, hwid}. Primera vez: liga HWID + activa el reloj.
    Devuelve el cochi.lic firmado con el hwid/techo/activacion reales."""
    data = request.get_json(silent=True) or {}
    key, hwid = (data.get("key") or "").strip(), (data.get("hwid") or "").strip().lower()
    if not key or not hwid_valido(hwid):
        return jsonify(error="key/hwid invalidos"), 400
    lic = find(key)
    if not lic:
        return jsonify(error="key no existe"), 404
    if lic["revocada"]:
        return jsonify(error="key revocada"), 403
    if lic["hwid"] and lic["hwid"] != hwid:
        return jsonify(error="key ligada a OTRO pc"), 403
    if lic["tipo"] != "hwid" and lic["tipo"] != "trial":
        return jsonify(error="tipo invalido"), 400

    hoy = date.today().isoformat()
    primera_vez = lic["hwid"] is None
    if primera_vez:
        q("UPDATE licencias SET hwid=?, activado=? WHERE key=?", (hwid, hoy, key),
          commit=True)
        activado = hoy
    else:
        activado = lic["activado"] or hoy

    # calcular vencimiento efectivo igual que license.hpp (dias absolutos)
    def days_from_iso(s):
        y, m, d = map(int, s.split("-"))
        return _days_from_civil(y, m, d)
    eff_days = days_from_iso(activado) + (lic["duracion"] or 0)
    if lic["techo"]:
        eff_days = min(eff_days, days_from_iso(lic["techo"]))
    elif lic["vence"]:
        eff_days = min(eff_days, days_from_iso(lic["vence"]))
    ey, em, ed = _civil_from_days(eff_days)

    payload = {
        "k": key, "t": lic["tipo"], "h": hwid,
        "e": f"{ey:04d}-{em:02d}-{ed:02d}",
        "n": lic["cliente"] or "", "i": lic["creada"] or int(time.time()),
    }
    if lic["duracion"]:
        payload["d"] = lic["duracion"]
        payload["a"] = activado
    return jsonify(lic=sign_lic(payload), primera_vez=primera_vez,
                   vence=payload["e"], tipo=lic["tipo"])


@app.post("/check")
def check():
    """Revalidacion en caliente del cheat: {key, hwid} -> valid true/false."""
    data = request.get_json(silent=True) or {}
    lic = find((data.get("key") or "").strip())
    hwid = (data.get("hwid") or "").strip().lower()
    if not lic or lic["revocada"] or not hwid_valido(hwid) or lic["hwid"] != hwid:
        return jsonify(valid=False), 200
    if lic["techo"] and date.today().isoformat() > lic["techo"]:
        return jsonify(valid=False, motivo="expirada"), 200
    return jsonify(valid=True, vence=lic["techo"] or lic["vence"]), 200


@app.post("/trial")
def trial():
    """Trial self-service: {hwid} -> 1 trial de 3 dias por HWID, ligado."""
    hwid = ((request.get_json(silent=True) or {}).get("hwid") or "").strip().lower()
    if not hwid_valido(hwid):
        return jsonify(error="hwid invalido"), 400
    if q("SELECT 1 FROM licencias WHERE hwid=? AND tipo='trial'", (hwid,), one=True):
        return jsonify(error="este pc ya uso su trial"), 409
    key = new_key()
    techo = (date.today() + timedelta(days=TRIAL_DIAS + TRIAL_GRACIA)).isoformat()
    q("INSERT INTO licencias (key,tipo,duracion,techo,hwid,activado,cliente,creada)"
      " VALUES (?,'trial',?,?,?,?,?,?)",
      (key, TRIAL_DIAS, techo, hwid, date.today().isoformat(), "trial-web",
       int(time.time())), commit=True)
    payload = {"k": key, "t": "trial", "h": hwid, "d": TRIAL_DIAS, "e": techo,
               "n": "trial-web", "i": int(time.time())}
    return jsonify(lic=sign_lic(payload), key=key, vence_techo=techo)


# ---------------------------------------------------------------- admin
@app.post("/admin")
def admin():
    """Operaciones del dueno: {token, op, ...}
    op=crear      {cliente, dias, hwid?}  -> key por activacion (techo auto)
    op=fecha      {cliente, dias}         -> key de fecha fija
    op=renovar    {key, dias}             -> suma dias desde HOY (techo nuevo)
    op=revocar    {key}                   -> bloquea la key (efecto inmediato)
    op=desrevocar {key}                   -> desbloquea
    op=reset_hwid {key}                   -> desliga el PC (activacion se conserva)
    op=eliminar   {key}                   -> borra el registro definitivamente
    op=listar"""
    data = request.get_json(silent=True) or {}
    if data.get("token") != ADMIN_TOKEN:
        return jsonify(error="token invalido"), 401
    op = data.get("op")
    key = (data.get("key") or "").strip()
    if op == "crear":
        key = new_key()
        dias = int(data.get("dias", 30))
        techo = (date.today() + timedelta(days=dias + int(data.get("gracia", 14)))).isoformat()
        q("INSERT INTO licencias (key,tipo,duracion,techo,hwid,cliente,creada)"
          " VALUES (?,'hwid',?,?,?,?,?)",
          (key, dias, techo, data.get("hwid", "").strip().lower() or None,
           (data.get("cliente") or "sin-nombre").strip(), int(time.time())), commit=True)
        return jsonify(key=key, dias=dias, techo=techo,
                       nota="mandale la key; se activa cuando abra el cheat")
    if op == "fecha":
        key = new_key()
        dias = int(data.get("dias", 30))
        vence = (date.today() + timedelta(days=dias)).isoformat()
        q("INSERT INTO licencias (key,tipo,vence,cliente,creada)"
          " VALUES (?,'hwid',?,?,?)",
          (key, vence, (data.get("cliente") or "sin-nombre").strip(),
           int(time.time())), commit=True)
        return jsonify(key=key, vence=vence)
    if op == "renovar":
        lic = q("SELECT * FROM licencias WHERE key=?", (key,), one=True)
        if not lic:
            return jsonify(error="key no existe"), 404
        dias = int(data.get("dias", 30))
        hoy = date.today()
        # base de renovacion: si sigue vigente, desde su vencimiento actual;
        # si ya expiro, desde hoy (no pierde dias por renovar tarde)
        base = hoy
        if lic["techo"]:
            techo_viejo = date.fromisoformat(lic["techo"])
            if techo_viejo >= hoy:
                base = techo_viejo
        techo = (base + timedelta(days=dias)).isoformat()
        q("UPDATE licencias SET techo=? WHERE key=?", (techo, key), commit=True)
        return jsonify(ok=True, techo=techo,
                       nota="el cliente re-abre el launcher y se re-activa solo")
    if op == "revocar":
        q("UPDATE licencias SET revocada=1 WHERE key=?", (key,), commit=True)
        return jsonify(ok=True)
    if op == "desrevocar":
        q("UPDATE licencias SET revocada=0 WHERE key=?", (key,), commit=True)
        return jsonify(ok=True)
    if op == "reset_hwid":
        # desliga el PC: el proximo /activate liga el HWID nuevo.
        # la fecha de activacion se conserva (renovar NO reinicia el reloj).
        q("UPDATE licencias SET hwid=NULL WHERE key=?", (key,), commit=True)
        return jsonify(ok=True, nota="pc desligado; el cliente abre el launcher y se re-liga solo")
    if op == "eliminar":
        q("DELETE FROM licencias WHERE key=?", (key,), commit=True)
        return jsonify(ok=True)
    if op == "listar":
        return jsonify(licencias=q("SELECT * FROM licencias ORDER BY creada DESC"))
    return jsonify(error="op desconocida"), 400


# ------------------------------------------------------- fechas (Hinnant)
def _days_from_civil(y, m, d):
    y -= m <= 2
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _civil_from_days(z):
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + (3 if mp < 10 else -9)
    return y + (m <= 2), m, d


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
