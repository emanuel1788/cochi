# COCHI CS — License Server

API de licencias: activación automática por key, trials self-service (1 por PC),
revocación en vivo. Flask + SQLite, corre gratis en Render.

## Qué endpoints expone

| Endpoint | Quién lo llama | Qué hace |
|---|---|---|
| `POST /activate` | el launcher | `{key, hwid}` → liga el HWID, activa el reloj la 1ª vez, devuelve `cochi.lic` firmado. El launcher TAMBIÉN lo llama en cada arranque con licencia válida: sincroniza renovaciones y borra el `.lic` local si responde `403 key revocada` |
| `POST /trial` | el launcher | `{hwid}` → trial de 1 día ligado a ese PC (1 por HWID) |
| `POST /check` | el cheat (futuro) | revalidación en caliente `{key, hwid}` con `motivo` |
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
   - `SUPABASE_DB_URL` = (recomendado) URI de Supabase para que los datos sean
     PERSISTENTES — sin ella el server usa SQLite y Render borra `keys.db` en
     cada redeploy/reinicio. Ver sección "Persistencia con Supabase" abajo.

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

## Persistencia con Supabase (gratis, 5 minutos)

El disco de Render free es **efímero**: cada redeploy o reinicio borra
`keys.db` y todas las licencias registradas desaparecen. La solución es mover
la base de datos a Supabase (Postgres gestionado, free tier, los datos
quedan en SU infraestructura y sobreviven a todo):

1. Crea cuenta en https://supabase.com (con GitHub) → **New project**:
   nombre `cochi`, password de DB (guárdalo), región Virginia/Eastus.
   Espera ~2 min a que el proyecto quede listo.

2. Copia la URI de conexión: **Project Settings → Database → Connection
   string → URI**. Se ve así:
   ```
   postgresql://postgres:[TU-PASSWORD]@db.cohicolocoxyz.supabase.co:5432/postgres
   ```
   Sustituye `[TU-PASSWORD]` por tu password. Si tiene caracteres especiales
   (`@ # % &` etc.), URL-encódalos: `@` → `%40`, `#` → `%23`, `%` → `%25`.

3. Pega la URI como variable `SUPABASE_DB_URL` en Render (Dashboard →
   tu servicio → Environment → Add → pega → Save). Render redespliega solo.

4. **Verifica:** abre los logs del servicio en Render y busca la línea
   `[db] Supabase/Postgres conectado: db.xxx.supabase.co`. Después crea una
   key desde tu GUI (pestaña Server → Crear) y confírmala en Supabase
   (Table Editor → tabla `licencias`).

5. **Migración (solo si ya tenías clientes):** exporta el SQLite actual
   desde la pestaña Shell de Supabase (o pídemelo y lo hago):
   ```sql
   -- crea la tabla y pega aquí los INSERT de tu keys.db
   ```

Nota sobre el free tier de Supabase: si el proyecto no recibe consultas
SQL durante una semana, Supabase lo **pausa** (te avisa por email antes;
un click en la dashboard lo restaura con TODOS los datos intactos — nunca
borra nada hasta pasados 12 meses de pausa). Con clientes usando el cheat
habrá actividad a diario y no se pausa. Cuenta gratis: 2 proyectos, 500 MB
(suficiente para cientos de miles de licencias).

## Experiencia del cliente con este sistema

```
1. Abre el Launcher (descarga sola la carpeta de instalación)
2. Si no hay licencia: menu → [6] Licencia
3. Pega su key "COCHI-XXXX-XXXX" (1 sola vez, se recuerda el PC)
   — o pulsa trial y recibe 1 dia gratis al instante
4. [2] Ejecutar cheat
```

Sin archivos .lic por Discord, sin renombrar, sin mandar HWID, sin re-emisiones.

## Importante: el free tier de Render "se duerme"

Render apaga el server tras ~15 min sin tráfico y la primera petición tarda
~50 s en despertarlo. Para que el launcher nunca espere: crea una cuenta
gratis en https://uptimerobot.com y añade un monitor HTTP cada 10 min contra
la URL del server. Con eso queda despierto 24/7.

## Notas de seguridad

- Con `SUPABASE_DB_URL` la base de datos vive en Supabase (persistente);
  sin ella, `keys.db` en Render (se pierde en cada redeploy — solo para pruebas).
- El rate-limit lo pone Render/Cloudflare delante; para V1 es suficiente.
- El token admin NUNCA va en el repo ni en el launcher del cliente.
- `private.key`/`LICENSE_SEED` comprometidos = regenerar todo (genkeys + recompilar
  cheat + re-publicar). Tenerlo SOLO en la env var del server y en tu respaldo.
