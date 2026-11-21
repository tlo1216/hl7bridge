# Security policy

## Reporting a vulnerability

Please report security problems privately through GitHub's private
vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for a security
problem.

Include the version (`hl7bridge --version`), your OS, and the steps to
reproduce. Use synthetic data only. Never include real patient data in a
report.

This is a one-person project, so response times are best effort.

## Supported versions

hl7bridge is pre-1.0. Only the latest release, and `main`, get security
fixes.

## What to know before you run it

- **Synthetic data only.** hl7bridge has not had a security review for real
  patient data and is not a certified medical device.
- **Plain MLLP has no authentication and no encryption.** Anyone who can
  reach the listener's port can send it messages. `hl7bridge listen` binds to
  `127.0.0.1` by default. Do not expose it on a network. TLS is optional
  (`--tls-cert`, `--tls-key`); it encrypts, and it authenticates senders only
  with `--tls-ca` (mutual TLS). See `docs/threat-model.md` for what TLS does
  and does not cover. A real deployment would still add a VPN.
- **The FHIR client sends what it is given.** `fhir sync` writes to the
  server you name. Use a test server.
- **Secrets come from the environment**, never from the command line, so
  they do not show in process listings. `HL7BRIDGE_FHIR_APIKEY` is never
  logged.
- **Logs and error text hold no patient data.** This is enforced by
  `tests/unit/test_phi_logging.py`. Reports printed to stdout do show
  identifiers such as accessions and MRNs; treat that output as you would
  the data itself.
- The demo script publishes HAPI FHIR on `127.0.0.1` only.
