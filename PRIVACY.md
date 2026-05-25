# OctoPrint-BitBang Privacy Policy

_Last updated: 2026-05-25_

This document describes what data the OctoPrint-BitBang plugin sends to the `bitba.ng` cloud signaling service, what that service does and does not see, and what control you have over it.

## Summary

- **No account, no sign-up, no telemetry.** The plugin does not collect personal information, send usage data, or require registration.
- **`bitba.ng` is a signaling relay only.** It helps two peers find each other and, if necessary, relays *encrypted* WebRTC traffic. It cannot decrypt your video, OctoPrint UI, or credentials.
- **You can disable cloud signaling entirely.** Set **Enabled** to off in *Settings → BitBang* — the plugin will run in local-only mode and never contact `bitba.ng`.

## What `bitba.ng` sees

When BitBang is enabled, the plugin opens a long-lived signaling connection to `bitba.ng`. The following data is visible to the signaling service:

- **Public key and UID.** On first run, the plugin generates a local keypair. The private key never leaves your device. The public key — and a UID derived from it that becomes part of your shareable URL — are registered with `bitba.ng`.
- **Connection metadata.** Timestamps of registration and peer-connection attempts, and the IP addresses of clients attempting to connect to your URL (necessary to broker the peer-to-peer connection).
- **Encrypted relayed traffic (TURN fallback only).** If a direct peer-to-peer connection cannot be established (e.g. strict NAT or firewall), `bitba.ng` may relay the WebRTC stream via TURN. The relay sees ciphertext only — the DTLS-SRTP encryption keys are negotiated end-to-end between your OctoPrint host and the connecting browser.

## What `bitba.ng` does **not** see

- The contents of your video stream.
- The contents of HTTP traffic between the browser and OctoPrint (G-code, file names, settings, the PIN, etc.).
- Your OctoPrint API key or session cookies.
- Files on your OctoPrint host or printer.

Once a WebRTC session is established, all media and HTTP traffic flows over an encrypted channel (DTLS-SRTP for media, encrypted data-channel for HTTP). This is true whether the path is direct or relayed through TURN.

## Access control

Anyone who has your BitBang URL can reach your OctoPrint instance. To require a passcode on the remote URL, set a **PIN** in *Settings → BitBang*. The PIN is enforced on the OctoPrint host, not by `bitba.ng`.

## Data retention

The plugin itself does not retain data about you. For `bitba.ng`'s own retention practices regarding signaling metadata and TURN logs, see the [BitBang project page](https://github.com/richlegrand/bitbang).

## Disabling cloud signaling

To stop the plugin from contacting `bitba.ng`:

1. Go to *Settings → BitBang*.
2. Uncheck **Enabled**.
3. Save and restart OctoPrint.

Local network access to the live video stream continues to work without cloud signaling.

## Contact

Questions or concerns: [open an issue](https://github.com/richlegrand/OctoPrint-BitBang/issues) on the plugin repository.
