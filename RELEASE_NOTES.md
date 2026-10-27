# Release Notes

## 1.0.0 — Initial public release

Shelly Fleet is an open-source Python desktop application for managing up to 100 MQTT-connected Shelly plugs.

### Included

- Fleet overview with individual ON, OFF, TOGGLE, and status-refresh controls.
- Dedicated device pages with power, voltage, current, energy, live session plots, and CSV export.
- Multiple named MQTT broker profiles, including a Default broker and per-device assignment.
- Verified TLS and optional unencrypted MQTT, each with separately remembered ports.
- Independent broker sessions, device presence indicators, and stale-data safeguards.
- Searchable device list, activity log, and multi-tab offline Help guide.
- Empty application defaults; local-only configuration and optional OS credential storage.
- Generic documentation, Windows installation launchers, MIT license, and security guidance.

### Notes

- This package contains no configured broker accounts, credentials, device IDs, personal certificates, or real-device screenshots.
- TLS and plain-MQTT behavior should be tested in your own environment before production use.
- The application is an independent community project and is not official Shelly software.
