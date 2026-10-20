# Security Policy

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Use GitHub's private [Report a vulnerability](https://github.com/lncrawl/lightnovel-crawler/security/advisories/new) feature. This keeps the report confidential until a fix is released.

Include in your report:

- A clear description of the vulnerability and its impact
- Steps to reproduce or a minimal proof-of-concept
- The version(s) affected

We aim to acknowledge reports within **3 business days** and release a fix within **14 days** for confirmed issues, depending on severity.

## Scope

The following are **in scope**:

- Authentication and session handling in the web server
- Privilege escalation between user tiers
- Arbitrary code execution or path traversal via crafted input
- Sensitive data exposure (credentials, API keys, user data)

The following are **out of scope**:

- Vulnerabilities in third-party sites that lncrawl crawls
- Issues that require physical access to the machine
- Self-inflicted issues from running untrusted user sources (`user_sources` config)

## Supported versions

Only the latest release receives security fixes. If you are on an older version, please upgrade first.
