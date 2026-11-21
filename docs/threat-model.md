# Threat model

One page, for a pilot decision. hl7bridge is built and tested on synthetic
data only. It has not had a security review for real patient data, and this
page lists what to fix or wrap before it sees any.

## Assets

- **PHI in messages**: names, MRNs, dates of birth, accessions, report text.
- **The store**: a SQLite file holding parsed messages, raw quarantined
  bytes, and the direct-mode log.
- **FHIR credentials**: the API key or bearer token for the target server
  (`HL7BRIDGE_FHIR_APIKEY`, `HL7BRIDGE_FHIR_TOKEN`).
- **TLS private keys**, when `--tls-key` is used.
- **Integrity of the downstream record**: what reaches the FHIR server.

## Trust boundaries

1. Sending system to the MLLP listener (network, untrusted bytes).
2. Listener to the store and quarantine files on local disk.
3. hl7bridge to the FHIR server (network, credentials).
4. Operator shell and logs (stdout, stderr, log files, shell history).
5. The mapping module file (`--mapping-module`), which runs Python inside
   the listener.

## Threats, mitigations today, and gaps

| Threat | Mitigation today | Gap |
| --- | --- | --- |
| **MLLP spoofing**: anyone who can reach the port sends messages claiming to be a hospital | Binds to loopback by default and warns when it does not. Optional TLS (`--tls-cert`, `--tls-key`), with `--tls-ca` to require and verify a client certificate (mutual TLS). TLS 1.2 minimum. TLS without `--tls-ca` on a non-loopback bind prints a warning that senders are not authenticated. Failed handshakes are logged with peer and reason (once per peer address per minute, then a summary, and at most 20 lines a minute across all peers), counted, and the total is logged at a clean shutdown (not when a Windows process is terminated, which runs no code). Each accepted client certificate is logged by peer, SHA-256 fingerprint, serial and issuer, percent-encoded, at a level that `--quiet` does not hide; the subject and alternative names (which can name a person or hold a URI query) are logged only at DEBUG | The sending hospital is still named by MSH-3 and MSH-4, which the sender writes. No mapping from a client certificate to an allowed hospital. Any certificate that chains to `--tls-ca` is accepted, so use a CA that issues only to your senders. No allow list of source addresses. Plain MLLP stays available |
| **Eavesdropping or tampering on the wire** | TLS on the listener and on `send` and `replay`; senders verify the server certificate and host name, with no insecure fallback | Revocation is not checked. The private key is an unencrypted file on disk (encrypted keys are refused), so file permissions are its only protection. Certificates are read at start: rotating one needs a listener restart. `--tls` alone trusts every CA in the system store. Neither `--tls` nor `--tls-ca` consults the Windows Disallowed store (administrator-blocked certificates): `--tls-ca` narrows the trusted issuers, but enforcing an individual Windows block needs a verifier or policy that checks it. Host names match subject alternative names only, never the common name. Ciphers are Python's default cipher string for TLS 1.2 and newer; renegotiation is turned off. `--tls` without `--tls-ca` also honors the `SSL_CERT_FILE` and `SSL_CERT_DIR` environment variables, so whoever controls the sender's environment controls its trust store; the Python 3.13 strict X.509 checks are not switched on for it (the same as `--tls-ca`). A VPN is still advisable |
| **Malformed or oversized input** | Frames over 1 MB and bytes outside framing are quarantined and counted; failed TLS handshakes and plain bytes sent to a TLS port are logged and counted but not quarantined (the listener never reads them as a message). A sender that sends plain MLLP to a TLS port has already put its first message on the wire unencrypted: the listener cannot protect that, so fix the sender's configuration, and treat that message as exposed; bad messages get AE or AR with a reason code | No per-sender rate limit. Plain MLLP has no cap on connections. TLS has a handshake time limit, at most `--max-connections` (default 256) connections open at once, at most 64 waiting in a handshake and 4 of those from one address (the rest are closed and logged), and a fixed table of 1024 peers for failure logging. A frame over 1,000,000 bytes of payload is quarantined, never handed on, even when its terminator arrives in the same read. Quarantined junk is not size-limited and can fill the disk. On Windows, ordinary process termination skips the clean-shutdown steps |
| **PHI in logs** | Log records pass a redaction filter; ACK text carries a reason code and a field name, never the value; `send` and `replay` never print message values, and never print the free text (MSA-3, ERR) of a receiver's negative ACK unless `--show-ack-text` is given; TLS errors name the flag and the reason, never a value, key or content | Reports, `explain` and `to-fhir` print PHI by design. Debug output (`-vv`) is not a reviewed channel |
| **PHI in quarantine and store files** | Kept in a local SQLite file for diagnosis | Not encrypted at rest. No retention or purge policy. File permissions are the operating system defaults. Quarantined bytes hold every field the sender wrote |
| **FHIR credentials** | Read from the environment, never from a flag, so they stay out of the process list; sent only to a server you name; a warning for credentials over plain http; URLs redacted in logs | No secret store. A token in the environment is visible to the same user. No rotation help. Credentials travel over http if you choose http |
| **Replay**: a captured message is sent again | A duplicate (same hospital and control id) is acknowledged and counted, not stored twice; FHIR writes are keyed, so a resend updates in place | An ACK must be an ACK that answers the message sent (MSA-2 equals MSH-10, read the same way on both sides; `UNKNOWN` only in a negative ACK), so a stale AA is a protocol failure, not a delivery, and a message with an empty MSH-10 is not sent. A replayed message with a new control id is a new message. TLS does not stop an authenticated sender from resending. Nothing checks MSH-7 freshness |
| **Mapping module runs code** | Refused on a listener that is not loopback | Anyone who can edit the file or the command line runs code as the listener user |

## Out of scope

Host hardening, network segmentation, backups, disaster recovery, access
control inside the FHIR server, and the clinical safety of the mapping.

## Before a pilot

Use mutual TLS or a VPN; run the listener under a dedicated low-privilege
account; encrypt the disk that holds the store; set a retention rule for
quarantine; keep keys out of the repository and rotate them; and have the
setup reviewed by whoever owns PHI security at the site.
