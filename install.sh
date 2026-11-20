#!/bin/bash
# Instala tado_aa como servicio systemd (alternativa a Docker) en Debian/Ubuntu.
# Ejecútalo con tu usuario normal, SIN sudo: el script pide sudo solo para
# crear el servicio. Requiere: sudo apt install python3 python3-venv
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "No lo ejecutes como root ni con sudo: el servicio correría como root." >&2
    exit 1
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

case "$DIR" in
    *[[:space:]]*)
        echo "La ruta '$DIR' contiene espacios y systemd no la admite; mueve la carpeta." >&2
        exit 1
        ;;
esac

# Entorno virtual: Debian 12/13 no permite 'pip install' global (PEP 668).
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

# Carpeta de datos (token de tado) y configuración, privadas para tu usuario.
mkdir -p "$DIR/data"
chmod 700 "$DIR/data"
if [ ! -f "$DIR/.env" ]; then
    cp "$DIR/.env.example" "$DIR/.env"
fi
chmod 600 "$DIR/.env"

sudo tee /etc/systemd/system/tado_aa.service > /dev/null <<UNIT
[Unit]
Description=Tado Auto-Assist
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
Environment=TADO_DATA_DIR=$DIR/data
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/.venv/bin/python $DIR/tado_aa.py
Restart=on-failure
RestartSec=60

# Endurecimiento: sin privilegios nuevos y solo puede escribir en data/.
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DIR/data
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now tado_aa.service

echo "Instalado. La primera vez hay que autorizar el acceso a tado:"
echo "  journalctl -u tado_aa -f     (abre la URL que aparece en el log)"
