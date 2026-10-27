# Security policy

## Configuration privacy

The public project contains no configured MQTT brokers or devices. When users create a fleet, configuration is saved outside the source directory in:

```text
%APPDATA%\ShellyFleetDesktop\fleet_config.json
```

This local file may contain MQTT hostnames, usernames, device names and topic prefixes, and certificate **paths**. Treat it as private even though it does not store passwords. Passwords are held temporarily in memory or optionally saved to the operating system credential manager using `keyring`.

Do not commit local configuration, passwords, credential exports, certificates, private keys, private screenshots, logs, or CSV exports. Inspect staged Git files before every publication and check repository history if sensitive material was committed. `.gitignore` does not remove secrets already committed.

To create an isolated demonstration profile, set the `SHELLY_FLEET_DATA_DIR` environment variable to an empty folder before launching the application. The normal launcher uses persistent per-user data. Importing another configuration always requires a user-selected file and confirmation; passwords are not imported.

## MQTT and TLS

- Use TLS with certificate and hostname verification on internet-accessible MQTT servers.
- Plain MQTT sends passwords, messages, and commands without encryption. Limit it to trusted private networks or a properly configured VPN.
- Apply least-privilege broker topic ACLs and use strong, non-shared credentials when possible.
- Use an appropriate trusted CA for private TLS deployments. Never disable certificate validation as a workaround for errors.
- Do not retain ON/OFF commands. Confirm switch-state feedback after sending a command.
- MQTT broker connectivity does not prove a physical plug is online. Relay status is the last reported state and can be stale.
- Do not use this application as a safety-critical shutdown mechanism.

## Responsible reporting

For a suspected vulnerability, contact the repository maintainer privately rather than including exploitable details, credentials, or device identifiers in a public Issue. Do not share an active broker password in bug reports.
