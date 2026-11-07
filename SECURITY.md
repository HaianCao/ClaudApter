# Security policy

ClaudApter runs on your computer. Provider settings are stored in your user
profile, and API keys in a local `.env` file in plain text: protect that file
and never commit it. The setup page listens only on `127.0.0.1`, requires a
per-session token and rejects other origins. The optional gateway is
[Protoverta](https://github.com/HaianCao/Protoverta), which binds to loopback
only and has its own [security policy](https://github.com/HaianCao/Protoverta/blob/main/SECURITY.md).

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub security advisories](https://github.com/HaianCao/ClaudApter/security/advisories/new)
rather than a public issue, and do not include real API keys. Rotate any key
that may have been exposed.
