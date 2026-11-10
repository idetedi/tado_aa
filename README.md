# tado_aa (Tado Auto-Assist: geofencing, ventana abierta y límite de temperatura)

Script en Python que hace por ti tres cosas que la app de tado° solo hace sola con la suscripción Auto-Assist:

- **Geofencing**: pone la casa en modo HOME o AWAY según si los móviles están en casa.
- **Ventana abierta**: cuando tado detecta una ventana abierta en una zona, activa el modo ventana abierta (corta la calefacción de esa zona).
- **Límite de temperatura**: si alguien pone una zona por encima del máximo o por debajo del mínimo, la devuelve al límite.

Fork de [adrianslabu/tado_aa](https://github.com/adrianslabu/tado_aa), reescrito para funcionar con python-tado 0.19 y con el límite diario de la API de tado. Pensado para equipos **V3 / V3+**; tado X no está soportado.

## Importante: el límite de la API de tado

tado limita su API a **100 peticiones al día** sin Auto-Assist (20.000 con Auto-Assist) ([artículo de tado](https://help.tado.com/en/articles/12165739-limitation-for-rest-api-usage)). El script original preguntaba cada 10 segundos; ahora reparte las comprobaciones a lo largo del día para no pasarse:

| Funciones activadas | Horario activo | Peticiones por comprobación | Una comprobación cada |
| --- | --- | --- | --- |
| Las tres | 24 h | 2 | 32 min |
| Las tres | 06:00-24:00 | 2 | 24 min |
| Solo geofencing | 24 h | 2 | 32 min |
| Solo ventana + temperatura | 24 h | 1 | 16 min |
| Solo ventana + temperatura | 06:00-24:00 | 1 | 12 min |

Lo que esto significa en la práctica con 100 peticiones al día:

- El cambio casa/fuera puede tardar hasta media hora en aplicarse.
- Una ventana abierta solo se detecta si el aviso de tado sigue activo cuando toca comprobar; muchas veces no lo estará.
- Cada cambio que aplica el script gasta 1 petición (2 en el cambio casa/fuera) y cada arranque gasta 2. Por eso se reservan 10 al día (`TADO_REQUEST_RESERVE`). Evita reiniciar el contenedor sin necesidad.
- Si el cupo se agota, el script espera hasta que tado lo renueva y sigue solo.

Si necesitas reacción inmediata, la vía sin límite es el control local por HomeKit (por ejemplo con Home Assistant). Este script no lo hace.

## Instalación con Docker (recomendada)

Pensada para un servidor Linux con Docker y Docker Compose (por ejemplo Debian 13). No abre ningún puerto, así que no necesita Traefik ni Cloudflare.

```bash
git clone https://github.com/idetedi/tado_aa
cd tado_aa
cp .env.example .env        # ajusta lo que quieras; todo es opcional
mkdir -p data && chmod 700 data
docker compose up -d --build
docker compose logs -f
```

`data/` debe pertenecer al usuario con el que corre el contenedor (por defecto uid/gid 1000). Si tu usuario tiene otro id (`id -u`, `id -g`), ponlo en `PUID` y `PGID` dentro de `.env`.

### Primer arranque: autorizar el acceso

La primera vez, el log muestra una URL de `login.tado.com`. Ábrela en cualquier navegador, inicia sesión con tu cuenta de tado y acepta. El script continúa solo y guarda el token en `data/refresh_token`; no vuelve a pedirlo salvo que tado lo invalide. La URL caduca a los pocos minutos: si se pasa, el script genera otra.

### Probar sin riesgo

Pon `DRY_RUN=true` en `.env`. El script escribe en el log lo que haría (`[DRY-RUN] ...`) sin cambiar nada en tado. Sigue gastando las peticiones de lectura.

## Configuración

Todo se configura con variables de entorno (fichero `.env`). Los valores por defecto están en [.env.example](.env.example).

| Variable | Por defecto | Qué hace |
| --- | --- | --- |
| `ENABLE_GEOFENCING` | `true` | Activa el cambio casa/fuera. |
| `ENABLE_OPEN_WINDOW` | `true` | Activa el modo ventana abierta. |
| `ENABLE_TEMP_LIMIT` | `true` | Activa el límite de temperatura. |
| `MIN_TEMP` / `MAX_TEMP` | `5` / `25` | Límites en ºC. |
| `TEMP_LIMIT_OVERLAY_MODE` | `NEXT_TIME_BLOCK` | Cuánto dura la corrección: `NEXT_TIME_BLOCK`, `MANUAL` o `TADO_MODE`. |
| `TADO_DAILY_LIMIT` | `100` | Cupo diario de tu cuenta. Si tado informa de otro, se usa el de tado. |
| `TADO_REQUEST_RESERVE` | `10` | Peticiones reservadas para arranques y cambios. |
| `CHECK_INTERVAL` | `0` | Segundos entre comprobaciones. `0` = calculado según el cupo. |
| `ACTIVE_HOURS` | `00:00-24:00` | Franja en la que se comprueba. Admite cruzar la medianoche (`22:00-06:00`). |
| `TZ` | hora del sistema | Zona horaria de `ACTIVE_HOURS` y del log, p. ej. `Europe/Madrid`. |
| `DRY_RUN` | `false` | Modo simulación. |
| `NTFY_URL` / `NTFY_TOKEN` | vacío | Avisos por [ntfy](https://ntfy.sh): URL completa del tema y token opcional. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | vacío | Avisos por Telegram. Van siempre juntos. |
| `NOTIFY_DEVICE_NAMES` | `false` | Incluir en los avisos el nombre de los móviles que están en casa. |
| `ZONE_NAMES` | vacío | Nombres de zonas: `1=Salón,2=Dormitorio`. Sin esto se ven como `Zone 1`. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` o `ERROR`. |
| `TADO_DATA_DIR` | `data` | Carpeta del token. En Docker es `/data`. |

Los avisos se envían cuando el script cambia algo, cuando hace falta volver a autorizar, cuando se agota el cupo y tras 5 comprobaciones fallidas seguidas.

## Instalación sin Docker (systemd)

En Debian/Ubuntu, con tu usuario normal (sin `sudo`):

```bash
sudo apt install python3 python3-venv git
git clone https://github.com/idetedi/tado_aa
cd tado_aa
./install.sh
journalctl -u tado_aa -f    # aquí aparece la URL de autorización
```

`install.sh` crea un entorno virtual en `.venv` (Debian no permite `pip install` global), copia `.env.example` a `.env` y registra el servicio `tado_aa` con tu usuario. Tras editar `.env`: `sudo systemctl restart tado_aa`.

En Windows se puede lanzar a mano para pruebas (no está probado allí): `py -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`, `.venv\Scripts\python tado_aa.py`, con las variables definidas en la consola.

## Seguridad

- `data/refresh_token` da acceso completo a tu cuenta de tado (calefacción y ubicación de los móviles). El script lo guarda con permisos solo para tu usuario y `.gitignore` evita subirlo. No lo compartas ni lo copies a otro sitio.
- Los secretos (tokens de ntfy y Telegram) van solo en `.env`, que tampoco se sube.
- El contenedor corre sin root, sin capacidades, sin puertos y con el sistema de ficheros en solo lectura.
- El log indica cuándo no hay nadie en casa y el nombre de los móviles. Los avisos no incluyen nombres salvo que actives `NOTIFY_DEVICE_NAMES`.
- Los avisos salen de tu servidor hacia ntfy o Telegram: usa un ntfy propio si no quieres que un tercero vea cuándo estás en casa.

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

Las pruebas usan los modelos reales de python-tado con la capa HTTP simulada: no hacen ninguna petición a tado. GitHub Actions las ejecuta en cada push.

`python-tado` está fijado a una versión concreta en `requirements.txt` porque cambia su interfaz entre versiones. Para actualizarlo, cambia la versión y ejecuta las pruebas.

## Créditos y licencia

Script original de [Adrian Slabu](https://github.com/adrianslabu/tado_aa). Usa [PyTado](https://github.com/wmalgadey/PyTado), de Chris Jewell y Wolfgang Malgadey. Licencia GPL-3.0 (ver [LICENSE](LICENSE)).

Si quieres dar las gracias al autor original: [Paypal](https://paypal.me/adrianslabu) / [Buymeacoffee](https://www.buymeacoffee.com/adrianslabu)
