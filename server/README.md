# COCHI CS — License Server

API de licencias: activación automática por key, trials self-service (1 por PC),
revocación en vivo. Flask + SQLite, corre gratis en Render.

## Qué endpoints expone

| Endpoint | Quién lo llama | Qué hace |
|---|---|---|
| `POST /activate` | el launcher | `{key, hwid}` → liga el HWID, activa el reloj la 1ª vez, devuelve `cochi.lic` firmado |
| `POST /trial` | el launcher | `{hwid}` → trial de 3 días ligado a ese PC (1 por HWID) |
| `POST /check` | el cheat (futuro) | revalidación en caliente `{key, hwid}` |
| `POST /admin` | tú | crear keys, revocar, listar (protegido con `COCHI_ADMIN_TOKEN`) |

## Despliegue en Render (10 minutos, gratis)

1. Los archivos del server ya están en este repo (`render.yaml`, `server/app.py`,
   `server/requirements.txt`). NO contienen ningún secreto: la clave de firma y el
   token admin van solo como variables de entorno en el dashboard de Render.
   (Si prefieres un repo aparte privado, también sirve.)

2. En https://dashboard.render.com → **New +** → **Blueprint** → conecta ese repo.
   Render lee `render.yaml` y crea el servicio `cochi-licenses`.

3. En las variables del servicio define:
   - `LICENSE_SEED` = el contenido de tu `C:\freebuff\licensing\private.key` **en hex**:
     ```
     python -c "print(open(r'C:\freebuff\licensing\private.key','rb').read().hex())"
     ```
     (es la MISMA clave que firmó tu pubkey.hpp: el cheat publicado verifica
     los .lic del server sin recompilar nada)
   - `COCHI_ADMIN_TOKEN` = una contraseña larga inventada para tu panel admin.

4. Deploy. Te da una URL tipo `https://cochi-licenses.onrender.com`.

5. En `launcher/` del proyecto (o donde compile el launcher), edita
   `lic_client.hpp` → `kServerBase` con esa URL y recompila el launcher.
   Ese Launcher.exe es el que le pasas a los clientes.

## Admin (desde tu PC, sin panel web)

```bash
# crear key de 30 dias (por activacion, techo automático)
curl -X POST https://TU-SERVER.onrender.com/admin -H "Content-Type: application/json" \
  -d '{"token":"TU_TOKEN","op":"crear","cliente":"juan","dias":30}'

# ver todo
curl -X POST https://TU-SERVER.onrender.com/admin -H "Content-Type: application/json" \
  -d '{"token":"TU_TOKEN","op":"listar"}'

# revocar una key filtrada (efecto inmediato en todos los clientes)
curl -X POST https://TU-SERVER.onrender.com/admin -H "Content-Type: application/json" \
  -d '{"token":"TU_TOKEN","op":"revocar","key":"COCHI-XXXX-XXXX"}'
```

## Experiencia del cliente con este sistema

```
1. Abre el Launcher (descarga sola la carpeta de instalación)
2. Si no hay licencia: menu → [6] Licencia
3. Pega su key "COCHI-XXXX-XXXX" (1 sola vez, se recuerda el PC)
   — o pulsa trial y recibe 3 dias gratis al instante
4. [2] Ejecutar cheat
```

Sin archivos .lic por Discord, sin renombrar, sin mandar HWID, sin re-emisiones.

## Importante: el free tier de Render "se duerme"

Render apaga el server tras ~15 min sin tráfico y la primera petición tarda
~50 s en despertarlo. Para que el launcher nunca espere: crea una cuenta
gratis en https://uptimerobot.com y añade un monitor HTTP cada 10 min contra
la URL del server. Con eso queda despierto 24/7.

## Notas de seguridad

- La DB `keys.db` vive en el server (Render la persiste en el disco del servicio;
  para persistencia garantizada usa Postgres free tier — migración trivial).
- El rate-limit lo pone Render/Cloudflare delante; para V1 es suficiente.
- El token admin NUNCA va en el repo ni en el launcher del cliente.
- `private.key`/`LICENSE_SEED` comprometidos = regenerar todo (genkeys + recompilar
  cheat + re-publicar). Tenerlo SOLO en la env var del server y en tu respaldo.
