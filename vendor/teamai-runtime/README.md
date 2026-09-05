# TeamAI isolated runtime lock

This directory is a consumer lock, not a fork or a vendored copy of TeamAI.

- Donor: `Tencent/teamai-cli`
- Reviewed donor commit: `6abfc69f454a2b84762cb84a6efcd9dc82f25d88`
- Runtime package: `teamai-cli@0.20.0`
- License: MIT
- Security resolutions: `simple-git@3.36.0`, `js-yaml@3.15.1`

Use `npm ci --ignore-scripts` in an isolated runtime directory. Do not install
globally and do not commit `node_modules`. ProjectContinuity verifies the exact
package, integrity, registry origin, and security overrides before a deployment
may install it. This lock does not install or configure an agent host.

`project-continuity-literal-recall.mjs` is ProjectContinuity adapter code, not
copied TeamAI source. It passes one bounded JSON query to the pinned donor's
native `recall` action without exposing TeamAI's sibling CLI subcommands. Keep
that wrapper in the immutable release and rerun the literal-query tests whenever
the TeamAI coordinate changes.
