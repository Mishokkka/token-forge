# Changelog

## 1.8.0 (unreleased)

- Single global settings schema for GUI and automation; explicit backup and recovery of malformed preferences.
- V3 output metadata records source content and render recipe fingerprints. Existing output skip policy is preserved; old and stale outputs carry warnings, not automatic overwrite authorization.
- Copy and extraction share a lazily captured immutable source snapshot. Pre/post-publication identity checks remain mandatory.
- Latest-only asynchronous composition and frame preparation; cached token geometry and item orientation; periodic full verification after cheap metadata checks.
- Pixel/encoded-size/format limits, model-revision-aware cache, byte-bounded memory retention, best-effort disk eviction and one inference session per process.
- Tagged artwork converted to sRGB; WebP copies retain the resulting profile; reject tokens with no visible subject.
- Per-file placement validation, non-blocking preferences lock acquisition and retry, Windows package constraints, dependency advisory workflow and actual Tk integration smoke.
- Previous hardening from PR #1: atomic publication, explicit overwrite consent, source identity and cache safety, SendTo ownership/migration, independent exports, Windows test matrix.

## 1.7.3

Console-less launcher stream initialization and improved error reporting.

## 1.7–1.7.2

Item composition, complete subject fitting, alpha-noise filtering, mirrors and independent adjustment controls.
