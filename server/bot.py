#!/usr/bin/env python3
# ============================================================================
# bot.py — Bot de Discord de COCHI CS (bilingue ES/EN)
#
# Estructura que arma !setup:
#   WELCOME   : bienvenida/welcome · precios/pricing · descargas/downloads ·
#               faq-es/faq-en
#   COMMUNITY : vouches (unico, bilingue) · referidos/referrals
#   SUPPORT   : soporte-es/support-en · bug-report · open-ticket (panel)
#   STAFF     : staff (oculto)
#
#   !setup      → crea roles, categorias, canales con permisos y los mensajes
#                 fijos en su idioma, y el panel de tickets:
#                 🛒 = ticket en espanol · 🌐 = ticket in English
#   🛒/🌐      → crea canal ticket-<usuario>-es|en visible solo para el
#                 cliente y vos, con planes/wallet/instrucciones en su idioma
#   !entregar   → cobra el plan, llama al server de Render (op=crear), publica
#                 la key en el ticket EN EL IDIOMA DEL TICKET, asigna el rol
#                 Cliente y registra la venta en ventas.json
#   !ventas     → resumen de ventas (cantidad + USDT por plan)
#   !keys !revocar !renovar !reset → gestion de licencias contra /admin
#
# Requisitos:  pip install -U discord.py requests
# Config:      la primera vez pregunta todo por consola y guarda config.json
#              (el ADMIN_TOKEN lo toma de licensing/server_token.txt si existe)
#
# Reacciona solo a comandos del OWNER (vos). Nadie mas puede tocar licencias.
# ============================================================================
import asyncio
import json
import os
import sys
import threading
from datetime import date

# Windows + aiodns exige SelectorEventLoop (el default de Python 3.8+ es Proactor)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import discord
from discord.ext import commands
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
VENTAS_PATH = os.path.join(HERE, "ventas.json")
SERVER_TOKEN_PATH = os.path.join(os.path.dirname(HERE), "licensing", "server_token.txt")

# plan -> (dias, precio_usdt, texto_corto_es, texto_corto_en)
PLANES = {
    "mes":     (30,   19.0, "1 mes",     "1 month"),
    "3meses":  (90,   49.0, "3 meses",   "3 months"),
    "6meses":  (180,  89.0, "6 meses",   "6 months"),
}
GRACIA_DIAS = 7  # dias de margen antes del techo absoluto

EMOJI_ES = "\U0001f6d2"                # 🛒 -> ticket en espanol
EMOJI_EN = "\U0001f310"              # 🌐 -> English ticket
CAT_ES = "ES"                             # sufijo de categorias en espanol
CAT_EN = "EN"                             # english category suffix

# layout del server: categoria -> [(canal, clave_texto|None, modo)]
#   ro    = solo lectura (info)   rw = escribible   panel = solo reacciones
LAYOUT = [
    (f"WELCOME {CAT_ES}", [
        ("bienvenida", "bienvenida", "ro"),
        ("funciones", "funciones", "ro"),
        ("precios", "precios", "ro"),
        ("pagos", "pagos", "ro"),
        ("descargas", "descargas", "ro"),
        ("faq-es", "faq", "ro"),
    ]),
    (f"WELCOME {CAT_EN}", [
        ("welcome", "welcome", "ro"),
        ("features", "features", "ro"),
        ("pricing", "pricing", "ro"),
        ("payments", "payments", "ro"),
        ("downloads", "downloads", "ro"),
        ("faq-en", "faq_en", "ro"),
    ]),
    ("COMUNIDAD", [
        ("referidos", "referidos", "ro"),
        ("referrals", "referrals", "ro"),
        ("vouches", "vouches", "rw"),
    ]),
    (f"SOPORTE {CAT_ES}", [("soporte-es", None, "rw")]),
    (f"SUPPORT {CAT_EN}", [("support-en", None, "rw")]),
    ("TICKETS", [
        ("open-ticket", None, "panel"),
        ("bug-report", "bug_report", "rw"),
    ]),
]


# ----------------------------------------------------------------- config
def load_config():
    if os.path.isfile(CONFIG_PATH):
        return json.load(open(CONFIG_PATH, encoding="utf-8"))
    return {}


def save_config(cfg):
    json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


def wizard():
    """Primera vez: pregunta lo minimo y escribe config.json."""
    print("== Configuracion inicial del bot COCHI CS ==\n")
    cfg = {}
    cfg["token"] = input("1) Token del bot (portal de Discord -> Bot -> Reset Token): ").strip()
    cfg["owner_id"] = input("2) Tu ID de usuario de Discord (clic derecho en tu nombre -> Copiar ID): ").strip()
    cfg["guild_id"] = input("3) ID del server (clic derecho en el icono del server -> Copiar ID): ").strip()
    cfg["wallet"] = input("4) Tu wallet USDT TRC-20 (la que mostrara en los tickets): ").strip()
    cfg["server_url"] = input(f"5) URL del server de licencias [{'https://cochi-licenses.onrender.com'}]: ").strip()
    if not cfg["server_url"]:
        cfg["server_url"] = "https://cochi-licenses.onrender.com"

    if os.path.isfile(SERVER_TOKEN_PATH):
        cfg["admin_token"] = open(SERVER_TOKEN_PATH, encoding="utf-8").read().strip()
        print(f"[+] ADMIN_TOKEN tomado de licensing/server_token.txt")
    else:
        cfg["admin_token"] = input("6) ADMIN_TOKEN del server de licencias: ").strip()

    save_config(cfg)
    print(f"\n[+] Config guardada en {CONFIG_PATH}")
    return cfg


CFG = load_config()
if not CFG.get("token") and os.environ.get("DISCORD_TOKEN"):
    # Modo hosting (Render): toda la config por variables de entorno, sin wizard.
    CFG = {
        "token": os.environ["DISCORD_TOKEN"],
        "owner_id": os.environ.get("DISCORD_OWNER_ID", ""),
        "guild_id": os.environ.get("DISCORD_GUILD_ID", ""),
        "wallet": os.environ.get("COCHI_WALLET", ""),
        "server_url": os.environ.get("SERVER_URL", "https://cochi-licenses.onrender.com"),
        "admin_token": os.environ.get("COCHI_ADMIN_TOKEN", ""),
        "panel_msg_id": os.environ.get("COCHI_PANEL_MSG_ID", ""),
    }
    print("[+] Bot en modo env vars (hosting remoto)")
if not CFG.get("token"):
    CFG = wizard()

ADMIN_TOKEN = CFG.get("admin_token", "")
SERVER_URL = CFG.get("server_url", "").rstrip("/")


# ----------------------------------------------------------------- api /admin
def api_admin(op, **kw):
    body = {"token": ADMIN_TOKEN, "op": op}
    body.update(kw)
    r = requests.post(f"{SERVER_URL}/admin", json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def registrar_venta(usuario, user_id, plan, key, usdt):
    ventas = []
    if os.path.isfile(VENTAS_PATH):
        ventas = json.load(open(VENTAS_PATH, encoding="utf-8"))
    ventas.append({
        "fecha": date.today().isoformat(),
        "discord": str(usuario),
        "discord_id": user_id,
        "plan": plan,
        "key": key,
        "usdt": usdt,
    })
    json.dump(ventas, open(VENTAS_PATH, "w", encoding="utf-8"), indent=2, ensure_ascii=False)


# ============================================================ TEXTOS ES / EN
# Todo lo que ve el cliente vive aca. Las claves se usan con T(lang, clave).
T_ES = {
    "bienvenida": (
        "**Bienvenido a COCHI CS**\n\n"
        "Cheat externo para CS2 orientado a juego legitimo: ESP, radar, evaluacion "
        "de enemigos (Leetify/FACEIT) y control de retroceso, con actualizaciones "
        "automaticas.\n\n"
        "- Info y planes -> #precios\n"
        "- Que tiene? -> #funciones\n"
        "- Para comprar o soporte privado -> reacciona con :shopping_cart: en #open-ticket\n"
        "- Proba gratis: trial de 1 dia automatico desde el Launcher (1 por PC)\n\n"
        "Reglas: respeto entre usuarios, nada de spam, nada de contenido de otros cheats.\n\n"
        ":globe_with_meridians: **English?** -> #welcome"),
    "welcome": (
        "**Welcome to COCHI CS**\n\n"
        "External cheat for CS2 focused on legit play: ESP, radar, enemy evaluation "
        "(Leetify/FACEIT) and recoil control, with automatic updates.\n\n"
        "- Info and plans -> #pricing\n"
        "- What's inside? -> #features\n"
        "- To buy or get private support -> react with :globe_with_meridians: in #open-ticket\n"
        "- Try it free: automatic 1-day trial from the Launcher (1 per PC)\n\n"
        "Rules: respect each other, no spam, no content from other cheats.\n\n"
        ":flag_ar: **Espanol?** -> #bienvenida"),
    "precios": (
        "**PRECIOS COCHI CS** — pago en USDT (red TRC-20)\n\n"
        "- Trial ......... GRATIS - 1 dia - automatico desde el Launcher (1 por PC)\n"
        "- 1 mes ......... 19 USDT\n"
        "- 3 meses ....... 49 USDT (equivale a 16.33/mes)\n"
        "- 6 meses ....... 89 USDT (equivale a 14.83/mes)\n\n"
        "**Como compro:**\n"
        "1) Reacciona con :shopping_cart: en #open-ticket\n"
        "2) En tu ticket te paso la wallet y pagas con USDT desde tu exchange o app crypto (guia completa en #pagos)\n"
        "3) Te entrego tu key al confirmar (2 minutos)\n\n"
        "Los dias NO corren al pagar: corren cuando abris el cheat por primera vez."),
    "pricing": (
        "**COCHI CS PRICING** — USDT payment (TRC-20 network)\n\n"
        "- Trial .......... FREE - 1 day - automatic from the Launcher (1 per PC)\n"
        "- 1 month ........ 19 USDT\n"
        "- 3 months ....... 49 USDT (equals 16.33/mo)\n"
        "- 6 months ....... 89 USDT (equals 14.83/mo)\n\n"
        "**How to buy:**\n"
        "1) React with :globe_with_meridians: in #open-ticket\n"
        "2) In your ticket I'll send the wallet; pay with USDT from any exchange or crypto app (full guide in #payments)\n"
        "3) Your key is delivered right after payment confirms (2 minutes)\n\n"
        "Days do NOT start when you pay: they start the first time you run the cheat."),
    "descargas": (
        "**DESCARGA COCHI CS**\n\n"
        "1) Entra al release: https://github.com/emanuel1788/cochi/releases/tag/distri\n"
        "2) Baja `Launcher.exe` (los demas archivos los baja solo el launcher)\n"
        "3) Abrilo: si ya compraste, pega tu key; si no, podes pedir el trial gratis\n\n"
        "Requisitos: Windows 10/11 x64 - CS2 instalado (Steam)\n"
        "Antivirus: puede marcar el launcher; es falso positivo por como funciona el "
        "overlay. Agregalo a exclusiones si te molesta el aviso."),
    "downloads": (
        "**DOWNLOAD COCHI CS**\n\n"
        "1) Go to the release: https://github.com/emanuel1788/cochi/releases/tag/distri\n"
        "2) Download `Launcher.exe` (everything else is downloaded by the launcher itself)\n"
        "3) Open it: if you already bought, paste your key; otherwise grab the free trial\n\n"
        "Requirements: Windows 10/11 x64 - CS2 installed (Steam)\n"
        "Antivirus: it may flag the launcher; it's a false positive due to how the "
        "overlay works. Add an exclusion if the warning bothers you."),
    "faq": (
        "**PREGUNTAS FRECUENTES**\n\n"
        "- **Es seguro? / Me van a banear?** Ningun cheat es 100% seguro. COCHI es "
        "externo y pensado para jugar legitimo (info, no aimbot agresivo). El riesgo "
        "existe siempre: usalo bajo tu decision.\n"
        "- **Hay prueba gratis?** Si: trial de 1 dia, automatico, 1 por PC, se pide "
        "desde el Launcher sin hablar con nadie.\n"
        "- **Compre y quiero pasarlo a otra PC** Escribi un ticket: se puede resetear "
        "el vinculo (1 vez por licencia).\n"
        "- **Se actualiza solo?** Si. El launcher compara versiones y baja lo nuevo solo.\n"
        "- **Perdi mi key** Abri un ticket con el nombre con el que compraste."),
    "faq_en": (
        "**FREQUENTLY ASKED QUESTIONS**\n\n"
        "- **Is it safe? / Will I get banned?** No cheat is 100% safe. COCHI is "
        "external and designed for legit play (info, not aggressive aimbot). Risk "
        "always exists: use it at your own decision.\n"
        "- **Free trial?** Yes: 1-day trial, automatic, 1 per PC, requested from the "
        "Launcher without talking to anyone.\n"
        "- **I bought and want to move to another PC** Open a ticket: the binding can "
        "be reset (once per license).\n"
        "- **Does it auto-update?** Yes. The launcher checks versions and downloads "
        "updates by itself.\n"
        "- **I lost my key** Open a ticket with the name you bought with."),
    "referidos": (
        "**TRAE UN AMIGO**\n\n"
        "Traes a alguien que compra cualquier plan -> 1 semana gratis para vos.\n"
        "Como funciona: tu amigo menciona tu usuario al abrir su ticket. Listo."),
    "referrals": (
        "**BRING A FRIEND**\n\n"
        "Bring someone who buys any plan -> 1 free week for you.\n"
        "How it works: your friend mentions your username when opening their ticket. Done."),
    "vouches": (
        "**VOUCHES / TESTIMONIALS**\n\n"
        ":flag_ar: Deja aca tu captura si el producto te anduvo bien. Los vouches sostienen el proyecto.\n"
        ":globe_with_meridians: Post your screenshot here if the product worked well for you. Vouches keep the project alive."),
    "funciones": (
        "**FUNCIONES DE COCHI CS** — todo lo que incluye tu licencia\n\n"
        ":dart: **AIMBOT LEGIT**\n"
        "- WindMouse: curvas de mouse humanas (gravedad, viento y decaimiento ajustables)\n"
        "- FOV dinamico: el area de asistencia se adapta a la distancia del objetivo\n"
        "- Perfil por clase de arma: FOV, suavizado y parametros propios para pistolas/SMG/rifles/snipers\n"
        "- Modos: solo al click o siempre activo · objetivo cercano o cabeza · hitbox 3D\n"
        "- Ignorar visibilidad (opcional) y sensibilidad del juego integrada\n\n"
        ":gun: **TRIGGERBOT & CONTROL DE RETROCESO**\n"
        "- Triggerbot con 4 modos (OFF/AUTO/HOLD/MAGNET) y delay por arma\n"
        "- Prediccion de movimiento del objetivo y margen de espera ajustable\n"
        "- RCS: control de retroceso automatico con multiplicadores pitch/yaw por clase de arma\n\n"
        ":eye: **ESP & VISUALES**\n"
        "- ESP caja + esqueleto, o modo solo-ruido con Sound Radar integrado\n"
        "- **Real Vis**: visibilidad REAL por raycast contra la geometria del mapa (con auto-deteccion de mapa y 16 mapas incluidos) — no el spotted basico de otros cheats\n"
        "- Snaplines, flechas fuera de pantalla, linea de mirada 3D, fondo de caja\n"
        "- Colores configurables al detalle (enemigo, aliado, radar) con picker HSV completo\n"
        "- Colores por tier, desvanecido por distancia, barra de vida fantasma, contorno de texto\n\n"
        ":bell: **INFO TACTICA**\n"
        "- Evaluacion de enemigos via Leetify: aim, accuracy, HS%, preaim y mas por cada jugador\n"
        "- Nivel y ELO de FACEIT por jugador, con colores por rango\n"
        "- Veredicto automatico: LEGIT / SOSPECHOSO / CHEATER basado en sus stats\n"
        "- Indicador de amenaza con radio ajustable y alerta de francotiradores\n"
        "- Alerta y estela de granadas · sonar de bomba · ESP de armas tiradas\n"
        "- Numeros de dano flotantes con fases de color, escala y distancia configurables\n"
        "- Deteccion de espectadores · cronometro y overlay propios\n\n"
        ":shield: **TU CUENTA, SEGURA**\n"
        "- **100% externo: sin inyecciones y sin escrituras en la memoria del juego** — driver de kernel propio de SOLO LECTURA\n"
        "- Mouse a nivel kernel: tus movimientos llegan como un mouse real de hardware (nada de inputs sinteticos)\n"
        "- No afecta tu Trust Factor ni la reputacion de tu cuenta\n"
        "- Overwatch safe: asistencia estilo legit + overlay Stream Proof — en repeticiones y transmisiones el juego se ve 100% natural\n"
        "- Ante cambios del juego salen updates rapidas para mantenerte siempre protegido\n\n"
        ":wrench: **CALIDAD DE VIDA**\n"
        "- 3 presets (LEGIT / SEMI-LEGIT / RAGE), guardado y carga de config\n"
        "- Menu in-game bilingue (espanol/ingles) con escalado\n"
        "- Teclas totalmente reasignables · Stream Proof (invisible en capturas)\n"
        "- Captura de log opcional para soporte · hitmarker sonoro\n\n"
        ":arrows_counterclockwise: **ACTUALIZACIONES AUTOMATICAS**\n"
        "- El launcher compara versiones y baja solo exe, mapas y recursos nuevos. No reinstalas nada, nunca.\n\n"
        "*En desarrollo constante: cada version suma funciones y mejoras. Mira #descargas para la ultima.*"),
    "features": (
        "**COCHI CS FEATURES** — everything included with your license\n\n"
        ":dart: **LEGIT AIMBOT**\n"
        "- WindMouse: human-like mouse curves (adjustable gravity, wind, decay)\n"
        "- Dynamic FOV: assist area adapts to target distance\n"
        "- Per-weapon profiles: separate FOV, smoothing and params for pistols/SMGs/rifles/snipers\n"
        "- Modes: on-click only or always-on · closest or head target · 3D hitbox\n"
        "- Optional visibility ignore + game sensitivity integration\n\n"
        ":gun: **TRIGGERBOT & RECOIL CONTROL**\n"
        "- Triggerbot with 4 modes (OFF/AUTO/HOLD/MAGNET) and per-weapon delay\n"
        "- Target motion prediction + adjustable grace period\n"
        "- RCS: automatic recoil control with pitch/yaw multipliers per weapon class\n\n"
        ":eye: **ESP & VISUALS**\n"
        "- Box + skeleton ESP, or sound-only mode with built-in Sound Radar\n"
        "- **Real Vis**: TRUE line-of-sight raycasts against map geometry (auto map detection, 16 maps included) — not the basic spotted flag other cheats use\n"
        "- Snaplines, offscreen arrows, 3D look line, box fill\n"
        "- Fully configurable colors (enemy, ally, radar) with full HSV picker\n"
        "- Tier colors, distance fade, ghost health bar, text outline\n\n"
        ":bell: **TACTICAL INFO**\n"
        "- Enemy evaluation via Leetify: aim, accuracy, HS%, preaim and more per player\n"
        "- FACEIT level and ELO per player, color-coded by rank\n"
        "- Automatic verdict: LEGIT / SUSPECT / CHEATER based on their stats\n"
        "- Threat indicator with adjustable radius + sniper warning\n"
        "- Grenade alert and trails · bomb sonar · dropped weapons ESP\n"
        "- Floating damage numbers with color phases, scale and distance settings\n"
        "- Spectator detection · session timer overlay\n\n"
        ":shield: **ACCOUNT SAFETY**\n"
        "- **100% external: no injections, no writes to game memory** — custom READ-ONLY kernel driver\n"
        "- Kernel-level mouse input: your aim reaches the game like a real hardware mouse (no synthetic inputs)\n"
        "- Does not affect your Trust Factor or your account reputation\n"
        "- Overwatch safe: legit-style assist + Stream Proof overlay — replays and streams show 100% natural gameplay\n"
        "- Fast updates whenever the game changes keep you protected\n\n"
        ":wrench: **QUALITY OF LIFE**\n"
        "- 3 presets (LEGIT / SEMI-LEGIT / RAGE), config save & load\n"
        "- Bilingual in-game menu (Spanish/English) with UI scaling\n"
        "- Fully rebindable keys · Stream Proof (hidden from captures)\n"
        "- Optional log capture for support · hitmarker sound\n\n"
        ":arrows_counterclockwise: **AUTOMATIC UPDATES**\n"
        "- The launcher checks versions and downloads new exe, maps and assets by itself. You never reinstall anything.\n\n"
        "*Constant development: every version adds features and improvements. Check #downloads for the latest.*"),
    "pagos": (
        "**CÓMO PAGAR — GUIA RAPIDA (USDT, red TRC-20)**\n\n"
        "USDT es una cripto estable: 1 USDT = 1 dolar. No se mueve de valor mientras compras.\n\n"
        ":one: **CONSEGUI USDT** — desde cualquier exchange o app crypto que ya conozcas "
        "(Binance, Bybit, OKX, y apps locales similares en tu pais): comprá USDT con tarjeta, "
        "transferencia o el metodo de pago que te ofrezcan\n"
        ":two: **RETIRA** — tocá Enviar/Withdraw -> elegí la red **TRC-20** -> pegá la wallet del ticket -> enviás\n"
        ":three: **LISTO** — la key llega al ticket en minutos\n\n"
        "Si nunca usaste crypto: crear una cuenta en un exchange toma ~10 minutos (verificas tu identidad "
        "una vez, como en un banco) y después comprar USDT son 2 clicks. Cualquier exchange grande sirve.\n\n"
        ":warning: **3 REGLAS DE ORO**\n"
        "- La red siempre **TRC-20** (si elegís otra, la plata se pierde)\n"
        "- Copiá la wallet **desde el ticket**, nunca de memoria\n"
        "- Enviar cuesta ~1 USDT de comisión; el monto que llega debe ser el exacto del plan\n\n"
        ":question: ¿Dudas? Abrí ticket y te guiamos paso a paso, en tu idioma."),
    "payments": (
        "**HOW TO PAY — QUICK GUIDE (USDT, TRC-20 network)**\n\n"
        "USDT is a stablecoin: 1 USDT = 1 dollar. Its value doesn't move while you buy.\n\n"
        ":one: **GET USDT** — from any major exchange or crypto app you already know "
        "(Binance, Bybit, OKX, or similar local apps): buy USDT with card, bank transfer or whatever they offer\n"
        ":two: **WITHDRAW** — tap Send/Withdraw -> pick the **TRC-20** network -> paste the ticket wallet -> send\n"
        ":three: **DONE** — your key lands in the ticket within minutes\n\n"
        "Never used crypto? Creating an exchange account takes ~10 minutes (one-time identity check, like a bank) "
        "and buying USDT is then 2 clicks. Any major exchange works.\n\n"
        ":warning: **3 GOLDEN RULES**\n"
        "- The network is always **TRC-20** (any other network = lost funds)\n"
        "- Copy the wallet **from the ticket**, never from memory\n"
        "- Sending costs ~1 USDT fee; the amount arriving must match the plan exactly\n\n"
        ":question: Questions? Open a ticket and we'll walk you through it in your language."),
    "bug_report": (
        "**REPORTE DE BUGS / BUG REPORTS**\n\n"
        ":flag_ar: Describi el problema y pegá la ultima parte de tu log (activa 'Capturar Log' "
        "en CONFIG & KEYS > DIAGNOSTICO antes de reproducirlo).\n"
        ":globe_with_meridians: Describe the issue and paste the last part of your log (enable 'Capture Log' "
        "in CONFIG & KEYS > DIAGNOSTICS before reproducing it)."),
    "panel": (
        "**TICKETS — COCHI CS**\n\n"
        ":shopping_cart: **Espanol** — reacciona aca para abrir un ticket privado de compra o soporte.\n"
        ":globe_with_meridians: **English** — react here to open a private ticket for buying or support.\n\n"
        "En el ticket vas a ver los planes y la wallet para pagar · In the ticket you'll see the plans and the payment wallet."),
    "ticket_es": (
        "{mention} gracias por tu interes en COCHI CS.\n\n"
        "**Planes (USDT, red TRC-20):**\n{planes}\n\n"
        "**Trial gratis:** 1 dia, automatico desde el Launcher, 1 por PC (sin hablar con nadie).\n\n"
        "**Para comprar:**\n"
        "1) Deci aca que plan queres\n"
        "2) Paga a esta wallet USDT (TRC-20): `{wallet}`\n"
        "3) Mandame aca la captura del pago\n"
        "4) Te entrego la key en el momento (los dias corren desde tu primera apertura)\n\n"
        ":globe_with_meridians: Want English? Just write here and I'll switch."),
    "ticket_en": (
        "{mention} thanks for your interest in COCHI CS.\n\n"
        "**Plans (USDT, TRC-20 network):**\n{planes}\n\n"
        "**Free trial:** 1 day, automatic from the Launcher, 1 per PC (no need to talk to anyone).\n\n"
        "**To buy:**\n"
        "1) Tell me here which plan you want\n"
        "2) Pay to this USDT wallet (TRC-20): `{wallet}`\n"
        "3) Send me the payment screenshot here\n"
        "4) Your key is delivered right away (days count from your first launch)\n\n"
        ":flag_ar: Prefieres espanol? Escribilo aca y cambiamos."),
    "entrega": (
        "**Tu licencia COCHI CS**\n\n"
        "```{key}```\n"
        "**Como activarla:**\n"
        "1) Baja `Launcher.exe` de #descargas\n"
        "2) Abrilo: te va a pedir la key la primera vez. Pegala y listo\n"
        "3) El resto (actualizaciones, mapas) es automatico\n\n"
        "Los dias corren desde tu primera apertura del cheat. Cualquier cosa escribi aca. "
        "Y si te anda bien, un vouch en #vouches se agradece."),
    "entrega_en": (
        "**Your COCHI CS license**\n\n"
        "```{key}```\n"
        "**How to activate it:**\n"
        "1) Download `Launcher.exe` from #downloads\n"
        "2) Open it: it will ask for your key the first time. Paste it and you're done\n"
        "3) Everything else (updates, maps) is automatic\n\n"
        "Days count from your first launch of the cheat. If you need anything, write here. "
        "And if it works well for you, a vouch in #vouches is appreciated."),
    "ya_tienes_ticket_es": "ya tenias un ticket abierto — por aca seguimos.",
    "ya_tienes_ticket_en": "you already had an open ticket — let's continue here.",
    "planes_header_es": "**Planes (USDT, red TRC-20):**",
    "planes_header_en": "**Plans (USDT, TRC-20 network):**",
}

T_EN = None  # (eliminado: todos los textos viven en T_ES con claves directas)

def T(key):
    """Texto por clave directa en T_ES (las claves *_en ya son su propia entrada)."""
    return T_ES.get(key, "")


def partir_texto(texto, limite=1900):
    """Divide un texto largo en mensajes <= limite chars, cortando por lineas
    (nunca por medio de una palabra). Discord rechaza mensajes > 2000."""
    if len(texto) <= limite:
        return [texto]
    bloques, actual = [], ""
    for linea in texto.split("\n"):
        while len(linea) > limite:            # linea gigante: corte duro
            bloques.append(linea[:limite])
            linea = linea[limite:]
        if len(actual) + len(linea) + 1 > limite:
            bloques.append(actual)
            actual = linea
        else:
            actual = f"{actual}\n{linea}" if actual else linea
    if actual:
        bloques.append(actual)
    return bloques


def planes_block(lang):
    idx = 3 if lang == "en" else 2
    lines = []
    for _, (d, precio, t_es, t_en) in PLANES.items():
        name = t_en if lang == "en" else t_es
        lines.append(f"- **{name}** — {precio} USDT")
    return "\n".join(lines)


# ----------------------------------------------------------------- bot
intents = discord.Intents.default()
intents.members = True          # para asignar el rol Cliente
intents.message_content = True  # para comandos con prefijo !

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def solo_owner():
    async def predicate(ctx):
        return ctx.author.id == int(CFG["owner_id"])
    return commands.check(predicate)


def es_owner(user) -> bool:
    return user.id == int(CFG["owner_id"])


async def canal_o_none(guild, nombre):
    return discord.utils.get(guild.text_channels, name=nombre)


def ticket_lang(channel) -> str:
    """Idioma de un ticket por su sufijo: ticket-<user>-es|-en. '' si no es ticket."""
    name = channel.name.lower()
    if name.startswith("ticket-"):
        if name.endswith("-en"):
            return "en"
        return "es"
    return ""


@bot.event
async def on_ready():
    user = bot.user
    # marker de conexion: app.py lo usa para detectar cuelgues pre-gateway
    try:
        with open(os.path.join(HERE, "conectado.flag"), "w") as f:
            f.write(str(user.id))
    except OSError:
        pass
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="CS2"))
    except discord.HTTPException:
        pass
    print(f"[+] Bot conectado como {user} (id {user.id})")
    gid = CFG.get("guild_id")
    if gid:
        guild = bot.get_guild(int(gid))
        if guild:
            print(f"[+] Server: {guild.name} — escribí !setup si aún no lo hiciste")
    print("[i] Comandos: !ayuda")


# ------------------------------------------------------------- !setup
@bot.command()
@solo_owner()
async def setup(ctx):
    """Crea roles, canales bilingues, permisos, mensajes fijos y el panel de tickets."""
    guild = ctx.guild
    if guild is None:
        return

    # 1) rol Cliente
    rol_cliente = discord.utils.get(guild.roles, name="Cliente")
    if rol_cliente is None:
        rol_cliente = await guild.create_role(
            name="Cliente", colour=discord.Colour.green(),
            reason="rol de clientes COCHI")
        await ctx.send("[+] Rol **Cliente** creado")

    everyone = guild.default_role
    over_ro = {
        everyone: discord.PermissionOverwrite(send_messages=False, create_public_threads=False,
                                              add_reactions=True, view_channel=True),
        rol_cliente: discord.PermissionOverwrite(send_messages=False, view_channel=True),
    }
    over_rw = {
        everyone: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        rol_cliente: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    over_panel = {everyone: discord.PermissionOverwrite(view_channel=True, send_messages=False)}

    async def get_cat(nombre):
        return (discord.utils.get(guild.categories, name=nombre)
                or await guild.create_category(nombre))

    async def upsert_channel(nombre, cat, over):
        """Crea el canal o, si ya existe, lo migra a la categoria/permisos del layout."""
        ch = await canal_o_none(guild, nombre)
        if ch is None:
            return await guild.create_text_channel(nombre, category=cat, overwrites=over)
        if ch.category_id != cat.id:
            await ch.edit(category=cat, overwrites=over)
        elif ch.overwrites_for(everyone).send_messages != over[everyone].send_messages:
            await ch.edit(overwrites=over)
        return ch

    async def send_once(ch, texto):
        """Publica el mensaje fijo solo si el bot no tiene ya uno en el canal
        (idempotente: re-correr !setup no duplica mensajes). Discord acepta
        max 2000 chars por mensaje: los textos largos se parten en bloques."""
        async for m in ch.history(limit=50):
            if m.author.id == bot.user.id:
                return
        for chunk in partir_texto(texto):
            await ch.send(chunk)

    # 2) estructura bilingue del LAYOUT (mueve canales existentes si hace falta)
    for cat_name, items in LAYOUT:
        cat = await get_cat(cat_name)
        for nombre, clave, modo in items:
            over = {"ro": over_ro, "rw": over_rw, "panel": over_panel}[modo]
            ch = await upsert_channel(nombre, cat, over)
            if clave:
                await send_once(ch, T(clave))

    # 3) limpieza de estructuras viejas (pre-bilingue y con banderas de pais)
    old = await canal_o_none(guild, "abrir-ticket")
    if old:
        await old.delete(reason="migrado a open-ticket")
    viejas_cat = ("WELCOME", "COMMUNITY", "SUPPORT",
                  f"WELCOME \U0001f1e6\U0001f1f7", f"WELCOME \U0001f1ec\U0001f1e7",
                  f"SOPORTE \U0001f1e6\U0001f1f7", f"SUPPORT \U0001f1ec\U0001f1e7")
    for vieja in viejas_cat:
        c = discord.utils.get(guild.categories, name=vieja)
        if c and not c.channels:
            await c.delete(reason="estructura actualizada")

    # 4) categoria STAFF (oculta)
    over_staff = {
        everyone: discord.PermissionOverwrite(view_channel=False),
        ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    c_st = await get_cat("STAFF")
    if not await canal_o_none(guild, "staff"):
        await guild.create_text_channel("staff", category=c_st, overwrites=over_staff)
    await c_st.edit(overwrites=over_staff)

    # 5) panel de tickets (bilingue, dos reacciones)
    ch_ticket = await canal_o_none(guild, "open-ticket")
    await ch_ticket.purge(limit=50)
    panel = await ch_ticket.send(T("panel"))
    await panel.add_reaction(EMOJI_ES)
    await panel.add_reaction(EMOJI_EN)
    CFG["panel_msg_id"] = panel.id
    CFG["guild_id"] = str(guild.id)
    save_config(CFG)

    await ctx.send(
        f"[+] Server actualizado (bilingue): categorias ES / EN, "
        "SOPORTE/SUPPORT separados por idioma y panel de tickets en #open-ticket.\n"
        "[i] 🛒 = espanol · 🌐 = English. Re-corre !setup cuando quieras: no duplica nada.")


@bot.command()
@solo_owner()
async def refresh(ctx):
    """Borra los mensajes fijos del bot en los canales de info y los vuelve a
    publicar con el texto actual (para actualizar contenidos sin tocar a mano).
    No toca tickets ni el panel."""
    guild = ctx.guild
    refrescados = []
    for cat_name, items in LAYOUT:
        for nombre, clave, modo in items:
            if not clave:
                continue
            ch = await canal_o_none(guild, nombre)
            if ch is None:
                continue
            await ch.purge(limit=100, check=lambda m: m.author.id == bot.user.id)
            for chunk in partir_texto(T(clave)):
                await ch.send(chunk)
            refrescados.append(f"#{nombre}")
    await ctx.send(f"[+] Textos re-publicados con el contenido actual: {', '.join(refrescados)}")


@bot.command()
@solo_owner()
async def panel(ctx):
    """Re-publica el panel de tickets (por si lo borran)."""
    ch = (await canal_o_none(ctx.guild, "open-ticket")
          or await canal_o_none(ctx.guild, "abrir-ticket"))
    if ch is None:
        return await ctx.send("[!] No existe #open-ticket; corre !setup primero")
    panel = await ch.send(T("panel"))
    await panel.add_reaction(EMOJI_ES)
    await panel.add_reaction(EMOJI_EN)
    CFG["panel_msg_id"] = panel.id
    save_config(CFG)
    await ctx.send("[+] Panel repuesto")


# ------------------------------------------------------------- tickets
@bot.event
async def on_raw_reaction_add(payload):
    if payload.user_id == bot.user.id:
        return
    lang = "es" if str(payload.emoji) == EMOJI_ES else ("en" if str(payload.emoji) == EMOJI_EN else "")
    if not lang:
        return
    if payload.message_id != int(CFG.get("panel_msg_id", 0)):
        return

    guild = bot.get_guild(payload.guild_id)
    user = payload.member or guild.get_member(payload.user_id)
    if guild is None or user is None or user.bot:
        return

    # un ticket por usuario: busca el sufijo nuevo y el legacy sin sufijo
    base = f"ticket-{user.name}".lower()
    existente = (discord.utils.get(guild.text_channels, name=f"{base}-{lang}")
                 or discord.utils.get(guild.text_channels, name=base))
    if existente:
        clave = "ya_tienes_ticket_en" if lang == "en" else "ya_tienes_ticket_es"
        await existente.send(f"{user.mention} {T_ES[clave]}")
        return

    over = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }
    owner = guild.get_member(int(CFG["owner_id"]))
    if owner:
        over[owner] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

    c_tickets = (discord.utils.get(guild.categories, name="TICKETS")
                 or discord.utils.get(guild.categories, name="SOPORTE"))
    ticket = await guild.create_text_channel(f"{base}-{lang}", category=c_tickets, overwrites=over)
    clave = "ticket_en" if lang == "en" else "ticket_es"
    await ticket.send(T_ES[clave].format(mention=user.mention,
                                         planes=planes_block(lang),
                                         wallet=CFG.get("wallet", "WALLET-SIN-CONFIGURAR")))
    # quitar la reaccion para permitir futuros tickets limpios
    try:
        ch = bot.get_channel(payload.channel_id)
        await ch.get_partial_message(payload.message_id).remove_reaction(
            payload.emoji, discord.Object(id=payload.user_id))
    except discord.HTTPException:
        pass


# ------------------------------------------------------------- ventas
@bot.command()
@solo_owner()
async def entregar(ctx, plan: str, usuario: discord.Member = None):
    """!entregar <mes|3meses|6meses> [@usuario] — crea la key en el server
    y la entrega en este canal en el idioma del ticket (usalo dentro del ticket)."""
    plan = plan.lower()
    if plan not in PLANES:
        return await ctx.send(f"[!] Plan invalido. Opciones: {', '.join(PLANES)}")
    if usuario is None:
        # dentro de un ticket: el dueño del ticket es el unico miembro que no sos vos
        members = [m for m in ctx.channel.members if not m.bot and m.id != int(CFG["owner_id"])]
        if not members:
            return await ctx.send("[!] Pase el ticket o usá: !entregar <plan> @usuario")
        usuario = members[0]

    dias, precio, t_es, t_en = PLANES[plan]
    try:
        resp = api_admin("crear", cliente=str(usuario), dias=dias, gracia=GRACIA_DIAS)
    except Exception as e:
        return await ctx.send(f"[!] Error llamando al server de licencias: {e}")

    key = resp.get("key", "")
    registrar_venta(usuario, usuario.id, plan, key, precio)

    # rol Cliente
    rol = discord.utils.get(ctx.guild.roles, name="Cliente")
    if rol:
        try:
            await usuario.add_roles(rol, reason=f"compro plan {plan}")
        except discord.HTTPException:
            pass

    await ctx.send(
        f"[+] Venta registrada: {usuario.mention} — {t_es} / {t_en} ({precio} USDT)")
    # entrega en el idioma del ticket; fuera de ticket, en ambos
    lang = ticket_lang(ctx.channel)
    if lang in ("es", ""):
        await ctx.send(T_ES["entrega"].format(key=key))
    if lang in ("en", ""):
        await ctx.send(T_ES["entrega_en"].format(key=key))


@bot.command()
@solo_owner()
async def stock(ctx, plan: str, n: int = 10):
    """!stock <mes|3meses|6meses> <n> — crea n keys de un plan para pegar como
    stock de serials en SellAuth (entrega automatica al cobrar). NO registra
    venta: son stock hasta que SellAuth las entrega."""
    plan = plan.lower()
    if plan not in PLANES:
        return await ctx.send(f"[!] Plan invalido. Opciones: {', '.join(PLANES)}")
    if not (1 <= n <= 50):
        return await ctx.send("[!] n entre 1 y 50.")
    dias, precio, t_es, t_en = PLANES[plan]
    fecha = date.today().isoformat()
    keys = []
    try:
        for i in range(n):
            resp = api_admin("crear", cliente=f"stock-sellauth {fecha} #{i + 1}",
                             dias=dias, gracia=GRACIA_DIAS)
            keys.append(resp.get("key", ""))
    except Exception as e:
        return await ctx.send(f"[!] Error al crear stock ({len(keys)}/{n}): {e}")
    await ctx.send(f"[+] Stock generado: {len(keys)} x {t_es} (${precio}) — pegalo en "
                   f"SellAuth como serials del producto. **No compartir fuera del dashboard.**")
    for trozo in partir_texto("```\n" + "\n".join(k for k in keys if k) + "\n```"):
        await ctx.send(trozo)


@bot.command()
@solo_owner()
async def ventas(ctx, n: int = 10):
    """Resumen de ventas registradas."""
    if not os.path.isfile(VENTAS_PATH):
        return await ctx.send("[i] Todavia no hay ventas registradas")
    v = json.load(open(VENTAS_PATH, encoding="utf-8"))
    total_usdt = sum(x["usdt"] for x in v)
    lineas = [f"- `{x['fecha']}` {x['discord']}: **{x['plan']}** ({x['usdt']} USDT) `{x['key']}`"
              for x in v[-n:]]
    await ctx.send(f"**Ventas: {len(v)} | Total: {total_usdt:.1f} USDT**\n" + "\n".join(lineas))


# ------------------------------------------------------------- licencias
@bot.command()
@solo_owner()
async def keys(ctx, n: int = 10):
    """Lista las ultimas licencias del server."""
    try:
        resp = api_admin("listar")
    except Exception as e:
        return await ctx.send(f"[!] Error: {e}")
    lics = resp.get("licencias", [])[:n]
    if not lics:
        return await ctx.send("[i] No hay licencias")
    lineas = []
    for l in lics:
        estado = "REVOCADA" if l.get("revocada") else ("activada" if l.get("activado") else "sin activar")
        lineas.append(f"- `{l['key']}` {l.get('tipo')} {l.get('duracion') or ''}d "
                      f"techo {l.get('techo') or '-'} · {estado} · {l.get('cliente') or '-'}")
    await ctx.send("\n".join(lineas))


@bot.command()
@solo_owner()
async def revocar(ctx, key: str):
    """Bloquea una key al instante (el cliente la pierde al proximo check)."""
    try:
        api_admin("revocar", key=key)
        await ctx.send(f"[+] `{key}` revocada")
    except Exception as e:
        await ctx.send(f"[!] Error: {e}")


@bot.command()
@solo_owner()
async def renovar(ctx, key: str, dias: int):
    """Suma dias a una key (techo y duracion)."""
    try:
        resp = api_admin("renovar", key=key, dias=dias)
        await ctx.send(f"[+] `{key}` renovada: nuevo techo {resp.get('techo')}. "
                       f"El cliente re-abre el launcher y se actualiza solo.")
    except Exception as e:
        await ctx.send(f"[!] Error: {e}")


@bot.command()
@solo_owner()
async def reset(ctx, key: str):
    """Desliga el PC de una key (cambio de maquina)."""
    try:
        api_admin("reset_hwid", key=key)
        await ctx.send(f"[+] `{key}` desligada; el cliente abre el launcher y se re-liga solo")
    except Exception as e:
        await ctx.send(f"[!] Error: {e}")


@bot.command()
async def ayuda(ctx):
    if es_owner(ctx.author):
        await ctx.send(
            "**Comandos owner:** !setup · !panel · !entregar <semana|mes|3meses|life> [@usuario] · "
            "!ventas [n] · !keys [n] · !revocar <key> · !renovar <key> <dias> · !reset <key>\n"
            "**Comandos todos:** !ayuda")
    else:
        await ctx.send(
            ":shopping_cart: Espanol: abri un ticket en #open-ticket · Trial gratis desde el Launcher.\n"
            ":globe_with_meridians: English: open a ticket in #open-ticket · Free trial from the Launcher.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return  # no es owner: silencio
    if isinstance(error, commands.CommandNotFound):
        return
    raise error


def run_bot():
    """Punto de entrada para hosting remoto (app.py lanza esto en un thread)."""
    owner_env = os.environ.get("DISCORD_OWNER_ID", "")
    print(f"[bot] pid={os.getpid()} thread={threading.current_thread().name} "
          f"owner_id_env={owner_env!r} guild_id_env={os.environ.get('DISCORD_GUILD_ID', '')!r} "
          f"cfg_owner={CFG.get('owner_id', '')!r}", flush=True)
    bot.run(CFG["token"])


if __name__ == "__main__":
    print(f"[bot] proceso separado pid={os.getpid()} | owner={CFG.get('owner_id')!r} "
          f"guild={CFG.get('guild_id')!r} | arrancando gateway...", flush=True)
    # loop manual con deadline: bot.run() puede quedar colgado para siempre sin
    # conectar ni loguear nada (se vio en Render con Cloudflare en el medio).
    try:
        os.remove(os.path.join(HERE, "conectado.flag"))  # solo vale la sesion ACTUAL
    except OSError:
        pass
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        try:
            loop.run_until_complete(asyncio.wait_for(bot.login(CFG["token"]), timeout=90))
        except asyncio.TimeoutError:
            print("[bot] TIMEOUT: login/gateway no conecto en 90s", flush=True)
            sys.exit(3)
        loop.run_until_complete(bot.connect())
    finally:
        try:
            loop.run_until_complete(bot.close())
        except Exception:
            pass
