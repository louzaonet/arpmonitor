# arpmonitor

ARPMonitor parses arp command output and stores MAC and IP addresses into a SQLite database.

Each time a new MAC address is found, the result is stored in database and sends an email using default profile at msmtp.

Initially designed for OpenWRT.