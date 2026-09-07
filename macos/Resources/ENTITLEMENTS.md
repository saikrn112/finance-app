# Entitlements — what's in each file and why

**Do not put XML comments in the `.plist` files.** `codesign` hands entitlements to
AMFI's own simplified XML parser, which rejects comments outright:

```
Failed to parse entitlements: AMFIUnserializeXML: syntax error near line 12
```

`plutil -lint` accepts the same file happily, so the plist is not malformed — the
constraint is AMFI's, and it costs a confusing signing failure. Rationale lives
here instead.

---

## `Entitlements.plist` — the app shell

Empty, deliberately. Every entitlement weakens the hardened runtime, so each one
has to be earned by an observed failure rather than added because it might help.

Not present, and why:

| Entitlement | Why not |
| --- | --- |
| `com.apple.security.app-sandbox` | The app spawns a child interpreter and reads a user-chosen private plugins directory. Sandboxing changes both designs, so it's a decision to make before phase 5, not a flag to set now (plan §10 q3). |
| `com.apple.security.cs.disable-library-validation` | Belongs on the interpreter, not here — see below. Entitling the `.app` does nothing for the wheels' `dlopen` calls. |
| `com.apple.security.cs.allow-unsigned-executable-memory` | Some CPython extension modules need it; none of ours has so far. Add it only when an import actually fails, and name the module here. |

## `Entitlements-Interpreter.plist` — release variant

Applies to `Resources/backend/python/bin/python3.12`.

It needs its own file because **the interpreter, not the app shell, is the host
process that `dlopen`s every wheel's extension module**, and library validation is
decided by the host process's entitlements.

Empty: with a real Developer ID every bundled binary carries the same Team ID, so
library validation is satisfied and nothing needs disabling.

> **UNVERIFIED.** This machine has no Developer ID (`security find-identity -v -p
> codesigning` reports 0 identities), so the release path has never been
> exercised. Confirm it before relying on it.

## `Entitlements-Interpreter-adhoc.plist` — development only

`macos/scripts/sign.sh` selects this file only when the identity is `-`.

Adds `com.apple.security.cs.disable-library-validation`, measured rather than
assumed:

1. Signing every bundled `.so`/`.dylib` ad-hoc **with** `--options runtime`, then
   importing them from the ad-hoc-signed interpreter, failed at `dlopen` for all
   11 native packages:

   > code signature ... not valid for use in process: mapping process and mapped
   > file (non-platform) have different Team IDs

   An ad-hoc signature carries no Team ID, so library validation — which the
   hardened runtime turns on — has nothing to match and rejects every load.

2. Dropping `--options runtime` made all 26 imports pass. That rules out a bad
   wheel and pins the cause on the hardened runtime.

3. Adding this single entitlement to the interpreter made all 26 imports pass
   *while keeping* the hardened runtime. That's the combination worth using in
   development.

Delete the ad-hoc branch in `sign.sh` once a Developer ID confirms step 2 is
unnecessary. Don't let this file quietly become the release configuration.
