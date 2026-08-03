# Security

Please report vulnerabilities privately through GitHub Security Advisories.

This repository intentionally contains no production credentials, Base IDs,
record IDs, webhook URLs, host addresses, personal data, or exported reports.
Keep runtime values in environment variables or a local secret store and never
commit `.env`, logs, database dumps, screenshots, or report payloads.

Use a read-only database account. If SSH forwarding is enabled, use key-based
authentication and verify the server host key outside this project.

