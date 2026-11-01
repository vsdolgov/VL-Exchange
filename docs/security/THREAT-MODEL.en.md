# Threat model and protection status

[Русский](THREAT-MODEL.ru.md) · Draft / Experimental · 2026-11-01

| Property | Mechanism | Limit |
|---|---|---|
| Integrity | SHA-256 against a trusted reference, digest in signed license | SHA-256 alone does not authenticate the author |
| Authenticity | Ed25519, preconfigured trust root | Signed malicious code remains malicious |
| Execution rights | Signed execute permission, validity, installation keys; online grant | Does not replace commercial terms or count cameras |
| Archive confidentiality | SealedBox for the installation key | Runtime needs plaintext, accessible to the trusted administrator |
| Copying ciphertext alone | Installation-key binding | A full installation clone including secrets is not blocked |
| Third-party code isolation | Host/supervisor/worker requirement | Not implemented in the reference Python library |
| Obfuscation/compilation | Possible additional profile | Not implemented or required for base exchange |
| Weight protection | Include weights in envelope with their own license scope | Separately delivered models are not automatically protected |

The prototype offers substantially more than an open Python ZIP: a delivery copy without the key does not reveal included code/data, and license modification is detected before execution. It does not prevent reverse engineering by a trusted administrator. Bytecode, obfuscation and native extensions may increase analysis cost but do not replace cryptography and license management.

## Untrusted packages

The first pilot permits reviewed publisher code only. Supporting untrusted authors requires a separate execution boundary:

1. A supervisor checks origin, signature, manifest, permitted profile and a preapproved dependency set. Pip installation, setup scripts and downloads during package opening are prohibited.
2. Code runs in a separate worker under a dedicated non-root UID without Docker socket, host PID namespace, publisher/license-server keys or other solutions' secrets. The runtime image is digest-pinned and dependencies reviewed.
3. The root filesystem is read-only; inputs/package are read-only; temporary data lives on private tmpfs. No home-directory mounts, arbitrary host paths or devices. Restrict `/proc`, dumps and neighboring process IPC.
4. Worker networking is disabled by default. A trusted supervisor handles licensing; approved model services are reached through a narrow broker with allowlists, quotas and timeouts. Permission to produce event candidates does not grant business API access.
5. Limit CPU, RAM, PIDs, file size, input/output volume, event rate and call duration. The supervisor kills hung processes and rejects partial results. Seccomp and AppArmor/SELinux are required; containers do not guarantee defense against every kernel attack.
6. Inputs/outputs use strict JSON/typed contracts, without pickle or arbitrary Python objects. The supervisor checks sequencing, state/event schemas, lease and idempotency before publishing events. A worker-provided “license checked” flag is not trusted.

Process separation limits platform-key access but does not hide the scenario's own plaintext from itself or the administrator. Strong isolation of hostile tenants may require dedicated VMs/microVMs; this remains future work.

## Receiving-system checks before production

Required tests include writing outside tmpfs; accessing network and host; exhausting memory, CPU and PIDs; hanging; excessive output; malicious dependencies; invalid JSON; false events; lease expiry during a call; worker crash and recovery. These container-boundary tests have not been performed for the current bundle. Python unit tests do not substitute for them.

The online prototype is tested over loopback. Production needs HTTPS, durable replay/revocation stores, rotation, rate limits, monitoring and audit. Offline needs key/clock control and agreed migration policy. GPU/TPM/TEE, hostile-administrator resistance and industrial certification are unverified.

[Docker: seccomp](https://docs.docker.com/engine/security/seccomp/) documents syscall restrictions; it supports Linux-host requirements and is not evidence of an executed test.
