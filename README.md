# ARPMonitor

[Español](#español) · [English](#english)

---

## Español

ARPMonitor es un daemon pequeño para routers con OpenWrt que te avisa por correo cada vez que aparece en tu red una MAC que no había visto antes. Guarda MAC, IP y hostname en una base SQLite y manda el aviso con `msmtp`, usando la cuenta por defecto.

Lo escribí para mi GL.iNet Brume 3 (GL-MT5000, firmware basado en OpenWrt 21.02, 1 GB de RAM y 8 GB de eMMC), pero debería funcionar en cualquier OpenWrt con Python 3.

### Cómo funciona

Cada 30 segundos (configurable) lee dos cosas:

- `/proc/net/arp`, la tabla ARP del kernel. Es lo mismo que te enseña `arp -n`. En OpenWrt `arp` no es un binario, es una función de `/etc/profile`, así que desde un servicio no existe y por eso leo el fichero directamente.
- `/tmp/dhcp.leases`, los leases de dnsmasq. Con esto pillo los equipos que piden IP y se van antes de la siguiente lectura de ARP, y además saco el hostname.

Las dos fuentes están en RAM. Leerlas no toca la eMMC.

Lo que ya conoce lo tiene en memoria, y solo escribe en la base cuando entra una MAC nueva o cambia la IP de una conocida. Si la red está quieta, cero escrituras. No actualizo `last_seen` en cada ciclo precisamente por eso: no merece la pena gastar flash para saber que el portátil sigue ahí.

La primera vez que arranca con la base vacía, guarda lo que ve sin mandar correos. Si no, te llegaría un correo por cada cacharro de casa.

Si el correo falla (msmtp mal configurado, sin conexión, lo que sea), el aviso queda pendiente en la base y se reintenta en el siguiente ciclo. Tampoco se pierde si reinicias el router a mitad.

### Requisitos

- OpenWrt con `opkg` y procd
- `python3-light`, `python3-email`, `python3-sqlite3`
- `msmtp` configurado con una cuenta `default`

Gasta unos pocos MB de RAM.

### Instalación

En el router:

```sh
opkg update
opkg install python3-light python3-email python3-sqlite3 msmtp ca-bundle
```

Desde tu equipo, copia los ficheros. El `-O` hace falta porque el scp moderno usa SFTP y dropbear no lo trae:

```sh
scp -O arpmonitord.py root@192.168.8.1:/usr/bin/arpmonitord.py
scp -O arpmonitor.init root@192.168.8.1:/etc/init.d/arpmonitor
```

Otra vez en el router:

```sh
chmod +x /usr/bin/arpmonitord.py /etc/init.d/arpmonitor
vi /etc/init.d/arpmonitor        # cambia MAIL_TO
/etc/init.d/arpmonitor enable
/etc/init.d/arpmonitor start
```

### Configurar msmtp

`/etc/msmtprc` de ejemplo. Ajusta servidor, usuario y contraseña:

```
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
syslog         LOG_MAIL

account        aviso
host           smtp.example.com
port           587
from           router@example.com
user           router@example.com
password       cambia-esto

account default : aviso
```

Pon `chmod 600 /etc/msmtprc`, que lleva la contraseña. Uso `syslog` y no `logfile` para no escribir el log de msmtp en la eMMC.

Prueba que sale correo antes de arrancar el daemon:

```sh
printf 'Subject: prueba\n\nhola\n' | msmtp tu@correo.com
```

### Uso

El servicio arranca solo con el router. Lo normal es no tocarlo.

```sh
/etc/init.d/arpmonitor restart   # tras cambiar la configuración
logread -e arpmonitor            # ver qué está haciendo
```

En `/etc/init.d/arpmonitor` puedes cambiar:

| Variable   | Por defecto           | Para qué |
|------------|-----------------------|----------|
| `MAIL_TO`  | `tu@correo.com`       | a quién llegan los avisos |
| `INTERVAL` | `30`                  | segundos entre lecturas (mínimo 5) |
| `DB`       | `/root/arpmonitor.db` | dónde va la base |

Leer más a menudo no escribe más en disco, así que bajar el intervalo solo cuesta algo de CPU.

El script también acepta estas opciones si lo lanzas a mano:

```
--to DIRECCION        destinatario (obligatorio)
--interval SEGUNDOS   intervalo entre lecturas
--db RUTA             base SQLite
--leases RUTA         leases de dnsmasq (por defecto /tmp/dhcp.leases)
--arp-file RUTA       tabla ARP (por defecto /proc/net/arp)
--notify-first-run    avisar también de lo que haya en la primera lectura
```

### Consultar la base

```sh
opkg install sqlite3-cli
sqlite3 -header -column /root/arpmonitor.db \
  'SELECT mac, ip, hostname, iface, first_seen FROM hosts ORDER BY first_seen'
```

`iface` vale `dhcp` cuando la MAC solo se vio en los leases y no en ARP.

### Conservarlo tras actualizar el firmware

Si actualizas el firmware sin conservar la configuración, o el sysupgrade no lo incluye, pierdes el daemon y la base. Añade esto a `/etc/sysupgrade.conf`:

```
/usr/bin/arpmonitord.py
/etc/init.d/arpmonitor
/root/arpmonitor.db
/etc/msmtprc
```

Después tendrás que volver a hacer `/etc/init.d/arpmonitor enable` y reinstalar los paquetes de Python con `opkg`.

### Limitaciones

- Los móviles modernos usan MAC aleatoria por red, y algunos la rotan cada cierto tiempo. Cada rotación te va a llegar como dispositivo nuevo. Si te molesta, desactiva la MAC privada para tu WiFi en esos equipos.
- Un equipo con IP fija, sin DHCP, que conecte y se vaya entre dos lecturas no lo vas a ver. Con el intervalo por defecto es raro.
- Solo IPv4. Los leases de IPv6 (odhcpd) no los leo.
- El hostname lo elige el propio equipo al pedir IP, así que no me fío de él. Antes de llegar a la base, al log o al correo se queda en `[A-Za-z0-9._-]` y como mucho 63 caracteres. Las MAC, las IP y las interfaces que no tienen formato válido se descartan.
- Esto no es un IDS. Una MAC se falsifica en un segundo, así que tómalo como un aviso útil y no como control de acceso.

### Licencia

GNU Affero General Public License v3.0. Ver [LICENSE](LICENSE).

---

## English

ARPMonitor is a small daemon for OpenWrt routers that emails you whenever a MAC address it hasn't seen before shows up on your network. It keeps MAC, IP and hostname in a SQLite database and sends the alert through `msmtp`, using the default account.

I wrote it for my GL.iNet Brume 3 (GL-MT5000, OpenWrt 21.02 based firmware, 1 GB RAM, 8 GB eMMC), but it should work on any OpenWrt box with Python 3.

### How it works

Every 30 seconds (configurable) it reads two things:

- `/proc/net/arp`, the kernel ARP table. Same thing `arp -n` shows you. On OpenWrt `arp` isn't a binary, it's a shell function in `/etc/profile`, so it doesn't exist for a service. That's why I read the file directly.
- `/tmp/dhcp.leases`, the dnsmasq leases. This catches devices that grab an IP and leave before the next ARP read, and gives me the hostname.

Both live in RAM. Reading them doesn't touch the eMMC.

Known hosts are kept in memory, and the database only gets written when a new MAC shows up or a known one changes IP. Quiet network, zero writes. I don't update `last_seen` every cycle for exactly that reason: burning flash to learn the laptop is still there isn't worth it.

On first start with an empty database it records what it sees without sending mail. Otherwise you'd get one email per gadget in the house.

If sending fails (bad msmtp config, no uplink, whatever), the alert stays pending in the database and is retried on the next cycle. It also survives a reboot in the middle.

### Requirements

- OpenWrt with `opkg` and procd
- `python3-light`, `python3-email`, `python3-sqlite3`
- `msmtp` set up with a `default` account

A few MB of RAM.

### Install

On the router:

```sh
opkg update
opkg install python3-light python3-email python3-sqlite3 msmtp ca-bundle
```

From your machine, copy the files. You need `-O` because modern scp uses SFTP and dropbear doesn't ship it:

```sh
scp -O arpmonitord.py root@192.168.8.1:/usr/bin/arpmonitord.py
scp -O arpmonitor.init root@192.168.8.1:/etc/init.d/arpmonitor
```

Back on the router:

```sh
chmod +x /usr/bin/arpmonitord.py /etc/init.d/arpmonitor
vi /etc/init.d/arpmonitor        # set MAIL_TO
/etc/init.d/arpmonitor enable
/etc/init.d/arpmonitor start
```

### msmtp setup

Sample `/etc/msmtprc`. Change server, user and password:

```
defaults
auth           on
tls            on
tls_trust_file /etc/ssl/certs/ca-certificates.crt
syslog         LOG_MAIL

account        alert
host           smtp.example.com
port           587
from           router@example.com
user           router@example.com
password       change-me

account default : alert
```

Run `chmod 600 /etc/msmtprc`, it holds the password. I use `syslog` instead of `logfile` so msmtp doesn't write its log to the eMMC.

Check that mail goes out before starting the daemon:

```sh
printf 'Subject: test\n\nhello\n' | msmtp you@example.com
```

### Usage

The service starts with the router. You normally don't need to touch it.

```sh
/etc/init.d/arpmonitor restart   # after changing settings
logread -e arpmonitor            # see what it's doing
```

In `/etc/init.d/arpmonitor` you can change:

| Variable   | Default               | What for |
|------------|-----------------------|----------|
| `MAIL_TO`  | `tu@correo.com`       | who gets the alerts |
| `INTERVAL` | `30`                  | seconds between reads (min 5) |
| `DB`       | `/root/arpmonitor.db` | database location |

Reading more often doesn't write more to disk, so a lower interval only costs a bit of CPU.

The script also takes these options if you run it by hand:

```
--to ADDRESS          recipient (required)
--interval SECONDS    time between reads
--db PATH             SQLite database
--leases PATH         dnsmasq leases (default /tmp/dhcp.leases)
--arp-file PATH       ARP table (default /proc/net/arp)
--notify-first-run    also alert on whatever is there on the first read
```

### Querying the database

```sh
opkg install sqlite3-cli
sqlite3 -header -column /root/arpmonitor.db \
  'SELECT mac, ip, hostname, iface, first_seen FROM hosts ORDER BY first_seen'
```

`iface` is `dhcp` when the MAC was only seen in the leases, not in ARP.

### Keeping it across firmware upgrades

If you upgrade without keeping settings, or sysupgrade doesn't include these files, you lose the daemon and the database. Add this to `/etc/sysupgrade.conf`:

```
/usr/bin/arpmonitord.py
/etc/init.d/arpmonitor
/root/arpmonitor.db
/etc/msmtprc
```

Afterwards you'll still need to run `/etc/init.d/arpmonitor enable` again and reinstall the Python packages with `opkg`.

### Limitations

- Modern phones use a random MAC per network, and some rotate it every so often. Every rotation will show up as a new device. If that bugs you, turn off the private MAC for your WiFi on those devices.
- A device with a static IP, no DHCP, that joins and leaves between two reads won't be seen. With the default interval that's rare.
- IPv4 only. I don't read the IPv6 (odhcpd) leases.
- The hostname is picked by the device itself when it asks for an IP, so I don't trust it. Before it reaches the database, the log or the email it's cut down to `[A-Za-z0-9._-]`, 63 chars max. Malformed MACs, IPs and interface names are dropped.
- This isn't an IDS. A MAC can be spoofed in a second, so treat it as a handy heads-up, not access control.

### License

GNU Affero General Public License v3.0. See [LICENSE](LICENSE).
