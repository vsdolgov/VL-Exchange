# VL Exchange Security 0.1 — protected delivery

[Русский](SPECIFICATION.ru.md) · Draft / Experimental · 2026-11-01

An open interface does not require authors to disclose source code or weights. This profile defines encrypted VLMP/VLSP delivery and execution authorization; the public library is a test prototype. VL's internal block format is outside the cross-vendor interface.

## 1. Trust and scope

The publisher, system administrator, clock, trust configuration and receiving host are trusted. An outsider holding only a delivery and license without the installation private key must not obtain plaintext. Protection against an administrator extracting memory, replacing the runtime or cloning secrets is not claimed. TPM/HSM, attestation, obfuscation and enforced isolation are not implemented in the library. Separately delivered weights are not protected by a scenario envelope.

The administrator configures trust roots before opening packages. A publisher key supplied inside a package is not automatically trusted. Python runs with process privileges; encryption and signatures do not make it safe. Isolation requirements are defined in the [threat model](THREAT-MODEL.en.md).

## 2. Keys and cryptography

The publisher has an Ed25519 signing key. An installation has an X25519 decryption key pair and a separate Ed25519 pair for online proof of possession. The online server has a separate Ed25519 signing key. Private keys are excluded from deliveries. Publisher and server public keys travel through a trusted channel.

Encryption is a libsodium sealed box over the exact ZIP bytes, using X25519/XSalsa20-Poly1305. A sealed box does not authenticate the sender; the license signature binding the plaintext ZIP digest provides authenticity. Signatures cover exact UTF-8 JSON bytes without reserialization. JSON rejects duplicate keys, NaN and Infinity. Base64 is standard with padding. SHA-256 uses 64 lowercase hex characters. Primitive references: [libsodium](https://doc.libsodium.org/public-key_cryptography/sealed_boxes), [Ed25519 RFC 8032](https://www.rfc-editor.org/rfc/rfc8032.html).

## 3. Envelope

A directory contains `envelope.json` and `package.sealed`. Required envelope fields are `format="vl.exchange.protected"`, `version="0.1.0"`, `license` (Base64 of exact license bytes), `license_signature` (Base64 of the 64-byte signature), `ciphertext_file="package.sealed"`, and `ciphertext_sha256`. Other 0.1.0 fields are rejected. A private key must never appear in a manifest. The ciphertext digest detects corruption but alone does not authenticate a sender.

## 4. License

The signed message is ASCII `VL-EXCHANGE-LICENSE-V1`, one zero byte, then exact license bytes. Required fields:

| Fields | Contract |
|---|---|
| `format`, `version`, `audience` | `vl.exchange.license`, `0.1.0`, `vl.exchange.runtime` |
| `issuer_id`, `license_id`, `installation_id` | ASCII matching `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`: 1–128 characters, starting with a letter or digit |
| `recipient_key_sha256` | SHA-256 of the installation's 32 raw X25519 public-key bytes |
| `request_verify_key` | Base64 of the installation's 32 raw Ed25519 public-key bytes |
| `package_sha256` | SHA-256 of the exact original ZIP bytes |
| `manifest_path`, `manifest_sha256`, `package_format` | Safe relative manifest path, its digest, declared `vl.*` format |
| `mode` | `offline` or `online` |
| `issued_at`, `not_before`, `expires_at` | Integer Unix UTC seconds from 0 through 253402300799, excluding bool; `issued_at <= not_before < expires_at` |
| `permissions` | Exactly `["execute"]` |

Unknown fields are rejected. A license is valid if `issued_at <= now` and `not_before <= now < expires_at`. Repacking changes delivery identity. Commercial terms govern use and migration; this technical document grants no such rights. Camera quotas, billing and unrestricted installation migration are not implemented in v0.1.

## 5. Offline

The host verifies the license with a trusted publisher key, then checks format, audience, mode, validity, execute permission and installation-key binding. It decrypts the ZIP, verifies package digest, manifest digest/format and payload contract. Code import before these checks is prohibited. Validity is checked before every call and before accepting its result.

No network or VL cloud is required. Immediate remote offline revocation requires an additional channel: expiry and administrator policy apply. Clock rollback protection independent of the trusted administrator is not implemented.

## 6. Online

The receiving system configures the URL, TLS trust store and server key; a package-supplied URL is ignored. A signed grant is required before decryption. HTTP is allowed only in explicitly enabled loopback tests; operational integration requires HTTPS with certificate and hostname validation, without redirects.

The installation generates a random 32-byte nonce encoded as 64 lowercase hex characters. It signs ASCII `VL-EXCHANGE-REQUEST-V1`, a zero byte and exact request JSON bytes. Fields are `format="vl.exchange.online-request"`, `version="0.1.0"`, `audience="vl.exchange.runtime"`, `license_sha256`, `license_id`, `installation_id`, `recipient_key_sha256`, `package_sha256`, `nonce`, `issued_at`. The transport JSON contains Base64 `license`, `license_signature`, `request`, `request_signature`.

The server validates publisher license, validity, online mode, execute permission, revocation, installation signature, identities and digests, freshness and nonce uniqueness. The response contains Base64 `grant` and `signature`. The signature domain is ASCII `VL-EXCHANGE-GRANT-V1`, one zero byte, then grant bytes. Grant fields are `format="vl.exchange.online-grant"`, `version`, `audience`, `license_sha256`, `license_id`, `installation_id`, `recipient_key_sha256`, `package_sha256`, `nonce`, `issued_at`, `expires_at`, `permissions=["execute"]`. Maximum TTL is 300 seconds and cannot exceed license expiry. The reference server defaults to 60 seconds and permits a configured TTL of 1–300. Allowed `abs(server_now-request.issued_at)` defaults to 30 seconds and can be configured within 0–120. Repeated nonces for the same license_id are rejected; the replay cache holds up to 100000 entries, retaining each for `2*max_clock_skew+1` seconds. Hosts check `grant.issued_at <= now < grant.expires_at <= license.expires_at`.

The host verifies server signature, all bindings, nonce and validity. Network, signature or renewal failure blocks new calls; there is no automatic offline fallback. Revocation applies at the next issuance and no later than current grant expiry. The test server stores its replay cache in memory and loses it on restart; durable storage and TLS termination are not implemented as a production service.

## 7. Leases and results

`before_process()` renews the grant when needed and returns an immutable `ExecutionLease`. After computing, the host checks that call's lease before committing state, result or event. A newer grant cannot authorize a result completed after the original lease expired. Failed renewal prevents reuse of the previous authorization for new calls.

The guard neither interrupts arbitrary Python nor reverses external actions of running code. Therefore the recommended worker has no direct access to business systems: it returns event candidates to a trusted supervisor. The supervisor validates the lease and contract before publishing an event. Full process isolation is a platform responsibility, not an implemented library guarantee.

## 8. Extraction, limits and failures

Traversal/absolute paths, duplicate and symlink ZIP entries are rejected. Prototype limits are 128 MiB ZIP, 512 MiB expanded content, 1024 ZIP entries including directories, and 1 MiB metadata; these are implementation limits. The private temporary directory is removed on normal exit. Reliable erasure after crashes or in memory is not guaranteed; private tmpfs and restrictions on untrusted access to dumps and processes are required.

Required negative cases include wrong keys/signatures, modified license/ZIP/manifest/ciphertext, wrong audience/mode/permissions, expiry before and during calls, wrong grant nonce/identities, replay, online failure and revocation. Concrete run results are published separately from requirements.

## 9. Migrating a paid solution

The format provides technical portability; commercial terms must preserve use rights on the new compatible platform. In v0.1 the installation is key-bound: the publisher reissues envelope and license for the new installation while preserving the original ZIP identity. The old online license is revoked; offline migration requires agreed deactivation/expiry and administrator trust. Without a communication channel there is no proof that all old copies were destroyed. Automatic cross-vendor license clearing and a shared payment service are not implemented.
