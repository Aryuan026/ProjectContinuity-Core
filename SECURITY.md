# Security policy

## Supported version

Security fixes target the latest tagged `0.1.x` source release until a later
support policy is published.

## Report a vulnerability

Use GitHub private vulnerability reporting when enabled. Do not open a public
issue containing credentials, private paths, tokens, personal data, or an
exploitable proof against a live deployment.

## Security boundary

- The front is designed for loopback binding.
- Bearer tokens are owner-only files, one per principal.
- Principal, actor, and role are derived server-side.
- Request and evidence shapes are bounded and closed.
- HTTP redirects are rejected so credentials cannot follow a foreign Location.
- Stage updates require exact CAS revisions.
- Promotion uses deterministic identity and recoverable receipts.
- Store, backup, changelog, lock, config, credential, and Cognee paths reject
  unsafe symlink escape at their owned boundaries.
- Evidence is sanitized before donor transformation and archival.

Remote exposure, TLS termination, secret provisioning, operating-system service
accounts, and backup encryption belong to the operator's deployment boundary.
Do not expose the raw front or donor management ports to the public Internet.
