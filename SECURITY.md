# Security policy

ClaudApter runs on your computer. Provider settings are stored in your user
profile, and API keys in a local `.env` file in plain text: protect that file
and never commit it. The setup page listens only on `127.0.0.1`, requires a
per-session token and rejects other origins. The local gateway is
[Protoverta](https://github.com/HaianCao/Protoverta), which binds to loopback
only and has its own [security policy](https://github.com/HaianCao/Protoverta/blob/main/SECURITY.md).

## Supported versions

Security fixes are released for the latest minor version only.

| Version | Supported |
|---|---|
| 0.2.x | Yes |
| Older versions | No — update with the installer command |

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/HaianCao/ClaudApter/security/advisories/new).
Do not post credentials or exploit details in a public issue. Rotate any key
that may have been exposed. You will get an answer within a week; a fix is
released as soon as it is ready, and the advisory is published with it.
