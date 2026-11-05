#!/usr/bin/env python3
"""
arpmonitord: daemon que vigila la red local en OpenWrt.

Copyright (C) 2026 Jorge Louzao Penalva
Licencia: GNU AGPL v3. Ver LICENSE.

Fuentes, ambas en RAM (no tocan la eMMC):
  - /proc/net/arp: la tabla ARP del kernel, lo mismo que imprime `arp -n`.
    En OpenWrt `arp` no es un binario sino una funcion de /etc/profile que
    hace `cat /proc/net/arp`, asi que bajo procd no existe. Si /proc/net/arp
    no esta (otro sistema), se prueba con el comando `arp -n`.
  - /tmp/dhcp.leases (dnsmasq, tmpfs): leases DHCP vigentes. Cubre equipos
    que conectan y se van entre dos lecturas de ARP, y aporta el hostname.

Cada INTERVAL segundos compara con lo que ya conoce (en memoria) y solo toca
la base SQLite cuando aparece una MAC nueva o cambia la IP de una conocida.
Si no hay cambios, no hay escrituras. Cada MAC nueva dispara un correo via
msmtp (cuenta por defecto).

Para no perder avisos si falla el correo o se reinicia el daemon, cada host
guarda un flag `notified`. Al arrancar y en cada ciclo se reintentan los
pendientes.

Paquetes: opkg install python3-light python3-email python3-sqlite3 msmtp
"""

import argparse
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from email.message import EmailMessage

DEFAULT_DB = "/root/arpmonitor.db"
DEFAULT_INTERVAL = 30  # segundos entre lecturas
MIN_INTERVAL = 5
PROC_ARP = "/proc/net/arp"  # virtual, no toca la eMMC
LEASES_FILE = "/tmp/dhcp.leases"  # tmpfs (RAM), leerlo no toca la eMMC

LINE_RE = re.compile(
    r"(?P<ip>\d{1,3}(?:\.\d{1,3}){3}).*?"
    r"(?P<mac>(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})"
)
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9._@-]{1,15}$")  # IFNAMSIZ = 16 con el NUL
HOST_BAD_RE = re.compile(r"[^A-Za-z0-9._-]")
HOST_MAX = 63  # longitud maxima de una etiqueta DNS
IGNORED_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}
MAX_READ = 1 << 20  # 1 MiB: tope de lectura por fichero

stop = threading.Event()


# --- Saneado -------------------------------------------------------------
# Todo lo que llega de ARP y, sobre todo, de los leases lo controla en parte
# un cliente de la red (el hostname DHCP lo elige el propio equipo). Se valida
# a la entrada y otra vez antes de mandar el correo. Las consultas SQL van
# siempre con parametros, asi que el saneado es para logs y correo.

def clean_mac(value):
    """MAC en minusculas y formato aa:bb:cc:dd:ee:ff, o None si no vale."""
    mac = value.strip().lower()
    if not MAC_RE.match(mac) or mac in IGNORED_MACS:
        return None
    return mac


def clean_ip(value):
    """IPv4 canonica (sin ceros a la izquierda, octetos 0-255), o None."""
    parts = value.strip().split(".")
    if len(parts) != 4:
        return None
    for p in parts:
        if not p.isdigit() or not p.isascii() or len(p) > 3:
            return None
        if (len(p) > 1 and p[0] == "0") or int(p) > 255:
            return None
    return value.strip()


def clean_iface(value):
    """Nombre de interfaz valido segun Linux, o '?' si no lo es."""
    return value if IFACE_RE.match(value) else "?"


def clean_host(value):
    """
    Hostname apto para logs, base y cabeceras de correo: solo [A-Za-z0-9._-],
    lo demas se sustituye por '_', y como mucho HOST_MAX caracteres.
    '*' (sin hostname en dnsmasq) pasa a cadena vacia.
    """
    if not value or value == "*":
        return ""
    return HOST_BAD_RE.sub("_", value)[:HOST_MAX]


def log(msg):
    # procd redirige stderr a syslog (logread).
    print(msg, file=sys.stderr, flush=True)


def read_text(path):
    """
    Lee un fichero de texto sin reventar nunca por la codificacion: un byte
    no UTF-8 en un hostname tumbaria el daemon con UnicodeDecodeError.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(MAX_READ)


def read_arp(path):
    """Devuelve el texto de la tabla ARP: /proc/net/arp o, si no hay, `arp -n`."""
    try:
        return read_text(path)
    except OSError:
        pass
    out = subprocess.run(["arp", "-n"], capture_output=True, text=True,
                         errors="replace", timeout=10)
    if out.returncode != 0:
        raise RuntimeError("arp fallo: " + out.stderr.strip())
    return out.stdout


def parse_arp(text):
    """
    Devuelve {mac: (ip, iface)} ignorando entradas incompletas.
    Formato de /proc/net/arp y de `arp -n`:
        IP address  HW type  Flags  HW address  Mask  Device
    Flags 0x0 = entrada sin resolver.
    """
    found = {}
    for line in text.splitlines():
        m = LINE_RE.search(line)
        if not m:
            continue
        mac = clean_mac(m.group("mac"))
        ip = clean_ip(m.group("ip"))
        if mac is None or ip is None or "incomplete" in line.lower():
            continue
        cols = line.split()
        if len(cols) >= 3 and cols[2] == "0x0":
            continue
        found[mac] = (ip, clean_iface(cols[-1]))
    return found


def parse_leases(path):
    """
    Lee /tmp/dhcp.leases de dnsmasq. Formato por linea:
        <expiry> <mac> <ip> <hostname|*> <clientid|*>
    Devuelve {mac: (ip, hostname)}. Un expiry de 0 es lease infinito;
    los caducados se ignoran. Si el fichero no existe, devuelve {}.
    """
    leases = {}
    now = time.time()
    try:
        text = read_text(path)
    except OSError:
        return leases
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        expiry = parts[0]
        if not (expiry.isascii() and expiry.isdigit()):
            continue
        if int(expiry) != 0 and int(expiry) < now:
            continue
        # Descarta lineas raras (DUID en vez de MAC, IPv6, basura)
        mac, ip = clean_mac(parts[1]), clean_ip(parts[2])
        if mac is None or ip is None:
            continue
        leases[mac] = (ip, clean_host(parts[3]))
    return leases


def collect(arp_path, leases_path):
    """
    Junta ARP y leases en {mac: (ip, iface, hostname)}.
    Si una MAC esta en ambos, manda la IP de ARP (es lo que se ve ahora);
    el hostname sale siempre del lease.
    """
    try:
        arp = parse_arp(read_arp(arp_path))
    except (RuntimeError, subprocess.SubprocessError, OSError) as e:
        log("error leyendo arp: %s" % e)
        arp = {}
    leases = parse_leases(leases_path)

    merged = {}
    for mac, (ip, iface) in arp.items():
        merged[mac] = (ip, iface, leases.get(mac, ("", ""))[1])
    for mac, (ip, host) in leases.items():
        if mac not in merged:
            merged[mac] = (ip, "dhcp", host)  # visto solo por lease
    return merged


def open_db(path):
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE IF NOT EXISTS hosts (
               mac        TEXT PRIMARY KEY,
               ip         TEXT NOT NULL,
               iface      TEXT,
               hostname   TEXT NOT NULL DEFAULT '',
               first_seen TEXT NOT NULL,
               last_seen  TEXT NOT NULL,
               notified   INTEGER NOT NULL DEFAULT 1
           )"""
    )
    # Migracion desde versiones anteriores. Solo escribe si falta la columna.
    cols = [r[1] for r in db.execute("PRAGMA table_info(hosts)")]
    if "notified" not in cols:
        db.execute("ALTER TABLE hosts ADD COLUMN notified INTEGER NOT NULL DEFAULT 1")
    if "hostname" not in cols:
        db.execute("ALTER TABLE hosts ADD COLUMN hostname TEXT NOT NULL DEFAULT ''")
    db.commit()
    return db


def send_mail(to, mac, ip, iface, hostname, first_seen):
    # Segunda pasada de saneado: filas de versiones antiguas de la base no
    # pasaron por los filtros de entrada.
    mac = clean_mac(mac or "") or "?"
    ip = clean_ip(ip or "") or "?"
    iface = clean_iface(iface or "")
    hostname = clean_host(hostname or "")
    first_seen = re.sub(r"[^0-9: -]", "", first_seen or "")[:19]
    msg = EmailMessage()
    msg["To"] = to
    label = "%s (%s)" % (mac, hostname) if hostname else mac
    msg["Subject"] = "[ARPMonitor] MAC nueva en %s: %s" % (socket.gethostname(), label)
    msg.set_content(
        "Dispositivo nuevo detectado en la red.\n\n"
        "MAC:       %s\nIP:        %s\nHostname:  %s\nInterfaz:  %s\n"
        "Visto por primera vez: %s\n"
        % (mac, ip, hostname or "(desconocido)", iface, first_seen)
    )
    # -t lee destinatarios de las cabeceras; sin -a usa la cuenta default.
    subprocess.run(["msmtp", "-t"], input=msg.as_string(), text=True, check=True, timeout=60)


def notify_pending(db, to):
    """Manda los avisos pendientes. Un UPDATE por correo enviado."""
    rows = db.execute(
        "SELECT mac, ip, iface, hostname, first_seen FROM hosts WHERE notified = 0"
    ).fetchall()
    for mac, ip, iface, hostname, first_seen in rows:
        if stop.is_set():
            return
        try:
            send_mail(to, mac, ip, iface, hostname, first_seen)
        except Exception as e:  # msmtp caido, timeout o cabecera invalida: no tumbar el daemon
            log("correo fallido para %s (se reintenta): %s" % (mac, e))
            return  # si msmtp esta caido no insistimos con el resto
        db.execute("UPDATE hosts SET notified = 1 WHERE mac = ?", (mac,))
        db.commit()
        log("aviso enviado: %s %s" % (mac, ip))


def main():
    ap = argparse.ArgumentParser(description="Daemon ARPMonitor")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--to", required=True, help="destinatario de los avisos")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("--arp-file", default=PROC_ARP, help="tabla ARP (por defecto /proc/net/arp)")
    ap.add_argument("--leases", default=LEASES_FILE, help="fichero de leases de dnsmasq")
    ap.add_argument(
        "--notify-first-run",
        action="store_true",
        help="avisar tambien de los hosts de la primera lectura con la BD vacia",
    )
    args = ap.parse_args()
    if args.interval < MIN_INTERVAL:
        ap.error("--interval minimo: %d segundos" % MIN_INTERVAL)

    # Parada limpia con SIGTERM/SIGINT (procd manda SIGTERM).
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    db = open_db(args.db)
    # Cache en memoria (mac -> ip): aqui se decide si hay algo que escribir.
    known = {mac: ip for mac, ip in db.execute("SELECT mac, ip FROM hosts")}
    baseline = len(known) == 0 and not args.notify_first_run
    log("arpmonitord iniciado, %d hosts conocidos, intervalo %ds" % (len(known), args.interval))

    notify_pending(db, args.to)

    while not stop.is_set():
        seen = collect(args.arp_file, args.leases)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        changed = False

        for mac, (ip, iface, hostname) in seen.items():
            if mac not in known:
                # Con la BD vacia en el primer ciclo se guarda como ya avisado.
                notified = 1 if baseline else 0
                db.execute(
                    "INSERT INTO hosts (mac, ip, iface, hostname, first_seen, last_seen, notified)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (mac, ip, iface, hostname, now, now, notified),
                )
                known[mac] = ip
                changed = True
                log("MAC nueva: %s %s %s (%s)" % (mac, ip, hostname or "-", iface))
            elif known[mac] != ip:
                # Aprovechamos la escritura para refrescar el hostname si lo hay.
                db.execute(
                    "UPDATE hosts SET ip = ?, iface = ?, last_seen = ?,"
                    " hostname = CASE WHEN ? <> '' THEN ? ELSE hostname END"
                    " WHERE mac = ?",
                    (ip, iface, now, hostname, hostname, mac),
                )
                known[mac] = ip
                changed = True
                log("IP cambiada: %s -> %s" % (mac, ip))

        if changed:
            db.commit()  # un solo commit por ciclo
        if seen:
            baseline = False
        notify_pending(db, args.to)

        stop.wait(args.interval)

    db.close()
    log("arpmonitord parado")


if __name__ == "__main__":
    main()
