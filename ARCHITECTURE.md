# TokenForge architecture and regression boundaries

The four review rounds identified related families of defects, rather than one faulty function. The original one-image utility grew into an editor, a batch processor and a multi-process Explorer integration. Its boundaries must therefore treat mutable external state as a first-class input.

| Boundary | Invariant | Regression strategy |
| --- | --- | --- |
| Image ownership | A filename is not proof of ownership. Inspect the generated marker; V2 identifies the recorded source filename, legacy V1 requires an unambiguous stem. Do not automatically overwrite independent art. | Independent/case-sensitive names, collisions, orphaned exports, legacy markers. |
| Source identity | Cache keys and renders describe SHA-256-verified source bytes, not just timestamps. Decode a bounded spooled snapshot on a cache miss. GUI and CLI share the extraction policy. | Source replacement, cache corruption, snapshot replacement, cache reuse without model startup. |
| Publication | Encode first, validate, publish atomically where supported, distinguish success with warnings from failure, preserve unexpectedly displaced bytes. Never delete a cache entry after a failed read. | Concurrent writes, replacement failures, recovery paths, late source changes. |
| Async GUI | Tk owns widgets; the worker owns discovery, hashing and extraction. Immutable request settings and generation identifiers prevent stale events from selecting another file. Close waits for cleanup. | Queued export, stale preview, folder discovery, source disappearance, shutdown. |
| Settings | Persist explicit changes and resets against the latest settings under an interprocess lock. Corrupt preferences must not silently drive automated export. | Concurrent edits, stale resets, clamped widgets and lock contention. |
| OS integration | New installation creates owned SendTo links only. No new writes to shared shell registry branches. Migration removes named legacy TokenForge verbs only. | Real Unicode Windows shortcuts, both third-party install orders, legacy cleanup, unrelated shortcuts. |
| Local inference | The allowed model identifiers are shared and explicitly local; unknown/remote backends cannot be instantiated. | Invalid model on automatic entry, offline cache tests, dependency import checks. |
| Runtime support | Regular 64-bit CPython 3.11–3.13; a common Pillow constraint; all supported runtimes exercised in Windows CI. Missing suites cannot be reported as green. | Offline suite and real dependency installation for each supported interpreter. |

## Limits and deliberate choices

- A non-cooperating external editor cannot be universally locked out. A late source change is reported; the user may regenerate. Existing original WebP copies are never overwritten automatically.
- Filename collision discovery uses a directory name index with refresh on observable directory changes; publication rechecks relevant names and the destination. This cannot stop another application creating a colliding name after publication.
- The GUI keeps the lightweight saved-image viewing path. Full extraction is deferred until necessary; source changes detected during editable preview trigger a refresh.
- SendTo takes one additional menu step and is visible for non-image files; the entry point validates the selection. This removes the shared-registry design risk rather than attempting to predict future machine-wide registrations. Previously created shared registry parents are not removed blindly.
- Tests simulate difficult external races and Windows replacement failures. Real shortcut creation/readback is exercised in Windows CI, not actual Explorer click interaction or neural inference/model accuracy.

## Release checklist

1. Run the complete Windows test matrix and dependency import checks.
2. Install and upgrade using regular Python on a Windows desktop; check a path containing spaces, Cyrillic and exclamation marks.
3. Exercise actual Explorer SendTo commands, including unsupported/multiple selections, and remove links.
4. Test real inference with the supported local models, including the first model download, and compare visual results on representative art.
5. Test a large folder and network/unavailable source. Keep the UI responsive and avoid claiming that an unmeasured operation is fast.
