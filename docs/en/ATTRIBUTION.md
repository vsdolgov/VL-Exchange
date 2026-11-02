# Attribution & Branding profile

**Final project edition · 2026-11-03 · [Русский](../ru/ATTRIBUTION.md)**

An optional profile for retaining a model or scenario developer's identity as a package travels between platforms. A publisher can describe its own identity, separate model and scenario authors, a preferred credit, and display preferences. For example, a receiving platform can show `Analytics by Detector Lab` or include the authors in an export. The platform vendor is a separate party and must not be represented as the model or scenario author unless that is actually true. Multiple authors are supported.

## Status and scope

This is a final project edition, not an approved industry standard or a claim of production readiness. The package contains a declaration; a receiving platform must implement the profile to validate, display or propagate it. An older host can ignore the optional extension and continue to run the base package. Therefore an author who requires attribution must check the receiving platform's capabilities and applicable license before distribution. No claim is made that another vendor's platform supports this profile or that a host cannot disable attribution.

The exchange specifications are intended to be open without a commercial license. The legal licenses for this project's specifications, schemas and code remain pending; publication alone grants no proposed rights. A model or scenario can have its own terms. See the [license proposal](../governance/LICENSE-PROPOSAL.en.md).

## Declaration in a VLMP or VLSP manifest

Use the existing `extensions` object and the key `org.vl-exchange.attribution`. Its envelope uses `required: false` so hosts unaware of this extension remain compatible. The base package versions and core event schemas are unchanged. A package without attribution remains valid. When present, a supporting host validates `data` against [attribution.schema.json](../../attribution/attribution.schema.json) as well as the package's normal manifest, inventory and security requirements.

```json
{
  "required": false,
  "data": {
    "profile_version": "0.1",
    "publisher": {"id": "org.example.publisher", "name": "Example Publisher"},
    "authors": [
      {"id": "org.example.detector", "name": "Detector Lab", "role": "model_author"},
      {"id": "org.example.scenario", "name": "Scenario Studio", "role": "scenario_author"}
    ],
    "package_version": "1.0.0",
    "preferred_text": "Analytics by Detector Lab",
    "display": {"live_view": "recommended", "export": "recommended", "event_metadata": "recommended"},
    "white_label": "unspecified"
  }
}
```

The example is the value of `extensions["org.vl-exchange.attribution"]`, not an entire manifest. `package_version` must equal the containing manifest's `version`. Publisher and each author can optionally have an HTTPS `website` and a package-relative PNG `logo`. An optional `provenance` object can describe the origin and link to a source; `license_url` can link to the package terms. URLs are descriptive, HTTPS-only and must not contain credentials. They are not fetched by the profile. A logo is a package-relative path, never a remote image URL. The schema defines the exact field restrictions. The reference additionally rejects duplicate `(id, role)` author pairs, versions that differ from the manifest, URL credentials or non-default HTTPS ports, unsafe paths and invalid image data.

## Run the local example

From the repository root, after installing the [project dependencies](../../requirements.txt), run:

```sh
python tools/attribution_demo.py --output-dir /tmp/vl-attribution-demo
```

Use an empty or nonexistent output directory. The synthetic package is at [model/examples/attribution-channel-classifier](../../model/examples/attribution-channel-classifier). The demo separately extracts the same package for two local host implementations and compares their attribution and inference results; both hosts use the same runtime. It writes `reference-host.json`, `independent-host.json`, `event.json` with author information, and `demo-report.json`. It also writes `frame-original.png` and a distinct `frame-annotated.png` to demonstrate an overlay on a synthetic frame. The example produces a still frame, not a video or a production player, makes no network requests and does not establish support by another vendor's platform.

## Display and policy

`display.live_view`, `display.export` and `display.event_metadata` each accept `recommended`, `required` or `disabled`. They describe the publisher's declaration for a given channel; they do not by themselves grant rights, enforce a legal condition or make hiding technically impossible. `white_label` is `allowed`, `prohibited` or `unspecified`. A host and operator must separately assess the applicable license and available capabilities, including conflicts or missing terms. The reference validator requires `license_url` when any display channel is `required` or `white_label` is `prohibited`; it checks only the presence and safety of the URL, not the license contents or rights. A license link is not proof that permission has been granted.

- **Live:** a supporting host may show a small text credit or logo, by default near the bottom. The host adapts size and position to avoid obscuring detections, faces, time or other necessary information. It can include more than one author and distinguish platform credit from analytics authors.
- **Embedded export:** a supporting host can render the credit into a *derived* video or frame. Never change the original stream or original archive recording. Export and live display are separate choices.
- **Metadata:** a supporting host can carry publisher, authors and package version in an analytics export or API envelope. This is separate from the unchanged VLSP event schema: no new base event fields are implied.

These are requested integration behaviors, not evidence of every implementation or of external platform adoption. Display recommendations, commercial licensing, code protection and cryptographic signatures are distinct mechanisms; none substitutes for the others.

## Identity, integrity and offline safety

Publisher and author names are claims, not independently verified identities. A digest detects a change relative to a trusted reference but does not authenticate its author. The optional [protected-delivery profile](../security/SPECIFICATION.en.md) signs an entitlement containing the exact manifest and package digests, using a key supplied through a trusted channel. The publisher-to-key binding must be established independently: a key embedded in a package is not inherently trusted. Signing does not prove that software is safe, that a name owns a trademark, or that a platform will display a credit.

A logo must be an inventoried package file bound to its digest, subject to the package's safe-path and integrity checks. The experimental reference accepts only a restricted, non-interlaced 8-bit PNG (grayscale, RGB, grayscale-alpha or RGBA; IHDR/IDAT/IEND chunks), at most 256 KiB and 1024 × 1024 pixels; SVG and remote logo loading are excluded. Hosts should treat image data and text as untrusted, use bounded decoding, escape text in the target UI, and avoid loading active content. A missing or invalid declared logo is rejected by the reference validator; it must not trigger a network fetch. A package can omit a logo and use text. Operators control links; a package URL must not cause an automatic request. Neither video nor inference data is sent to third-party servers to display attribution. An installation can use packaged assets offline, without a cloud service.

Prepare and validate attribution at package load time and cache display data instead of adding work to every inference call. The prototype does not claim a measured performance overhead or provide a production video player. For base-package rules, see [VLMP](../../model/spec/SPECIFICATION.en.md), [VLSP](../../scenario/spec/SPECIFICATION.en.md) and the [integration guide](GUIDE.md).
