# SKU Scan — Android

Kotlin + XML views. **No Jetpack Compose.** Light theme only.

Targets the Zebra TC22. Capture is CameraX; EMDK is used for the device gate only; ZSDK will handle ZQ320 printing in Phase 5.

---

## Build

```powershell
cd android\dmart-ocr
.\gradlew assembleDebug
```

Requires JDK 17 (Android Studio's bundled JBR is fine). The Gradle wrapper is committed — no system Gradle needed.

### Configuration

`local.properties` is git-ignored and holds everything environment-specific:

```properties
sdk.dir=C:\\Users\\<you>\\AppData\\Local\\Android\\Sdk

dmart.backendUrl=http://10.0.2.2:8000/
dmart.apiKey=
```

`10.0.2.2` is the emulator's route to the host. On a real TC22 use the server's LAN address, e.g. `http://192.168.1.50:8000/`.

No URL, key, or path is hard-coded in source (CLAUDE.md §19).

---

## Screens

| Screen | Purpose |
| --- | --- |
| `MainActivity` | Home. Brand, device-gate state, server state, one action. |
| `ScanActivity` | CameraX preview with the fixed ROI window; crops and uploads. |
| `ResultActivity` | Confirm/edit extracted values, then commit. |

There is no login, onboarding, or settings screen.

`ResultActivity` is part of the scan flow rather than a navigation destination: §27 of the specification requires the operator to review, edit, and confirm before anything is committed, so the flow cannot end at the camera.

---

## Device restriction

The app runs only on Zebra hardware. `ZebraGate` checks, in order:

1. `Build.MANUFACTURER` contains `Zebra Technologies` **or** `Motorola Solutions` — older Zebra units are rebranded Motorola.
2. `PackageManager` resolves `com.symbol.emdk.emdkservice`, Zebra's EMDK runtime.

Two things that will waste a day if forgotten:

- The manifest **must** keep its `<queries>` element for `com.symbol.emdk.emdkservice`. Without it the package lookup fails on *every* device, including a genuine TC22, and the gate rejects everything while looking like a hardware fault.
- `ENFORCE_ZEBRA_ONLY` is `false` in debug and `true` in release. Without that bypass no development is possible until a TC22 is physically present. The bypass logs a loud warning and can never be active in a release build.

The EMDK Gradle dependency is `compileOnly` and currently commented out: the runtime ships on the device, and the package-level check needs no SDK artifact, so the build does not depend on Zebra repository access. Uncomment it when adding full `EMDKManager` initialization.

---

## Region of interest

The operator **never crops**. A fixed ROI window is drawn over the preview, they align the label to it, and the app crops the capture to that rectangle before upload.

`RoiOverlayView` is both the aiming guide and the definition of the crop, so the two cannot drift apart. It exposes `roiFraction()` — the rectangle as fractions of the view — which maps directly onto the captured bitmap.

That mapping is only valid because preview and capture are bound through a shared `ViewPort` in a `UseCaseGroup`. Without it the two use cases cover different fields of view, and preview coordinates applied to a full-resolution capture crop the wrong region — subtly wrong, which reads as an OCR failure rather than a bug.

Cropping is also the largest latency lever in the system: backend OCR time scales with the number of detected text regions, not image size, so keeping surrounding packaging out of the frame is worth real seconds per scan.

---

## Design

Palette is derived from the Markss mark in `assets/marks_info_logo.png`: ink `#0F1010`, cyan `#00A7E8`. The cyan is the only saturated colour in the interface, so it reads as "this is the action" wherever it appears.

Type is Google Sans (OFL), bundled at 400/500/700 and subset to Latin plus `₹` — 168 KB total, down from 5.7 MB unsubset.

Dynamic colour is deliberately off. The confidence bands carry safety meaning, and letting device wallpaper retint them would undermine the one thing the operator must read correctly. Each band is signalled three ways — coloured rail, chip background, and chip text — so it survives colour blindness and warehouse glare.

---

## Printing

ZQ320 over Bluetooth via the Zebra Link-OS Multiplatform SDK, ZPL, with a 10 mm x 10 mm QR.

The SDK is **not on Maven Central** — it is a manual download from Zebra behind an EULA. The dependency declaration is committed; the binary is not.

To build with printing:

1. Download the Link-OS Multiplatform SDK for Android from Zebra.
2. Copy `ZSDK_ANDROID_API.jar` into `app/libs/`.

`app/build.gradle.kts` picks up any jar in `libs/`, and `app/libs/*.jar` is git-ignored.

Why the SDK rather than a raw RFCOMM socket: `PrinterStatus`. A socket write to a printer that is out of labels or has its head open succeeds at the transport level and silently produces nothing — exactly the silent failure CLAUDE.md §18 forbids. Checking status first turns it into a message the operator can act on.

### QR density budget

Fixed by §17 and enforced in `ZplBuilder`:

| Setting | Value |
| --- | --- |
| Head | 203 dpi = 8 dots/mm |
| Symbol | 10 mm = 80 dots |
| `^BQ` magnification | 2 (module = 2 dots = 0.25 mm) |
| QR version | 5 — 37 x 37 modules = 74 dots = 9.25 mm |
| Error correction | M, ~154 alphanumeric characters |

Version 6 is 41 modules = 82 dots = 10.25 mm and overflows, so a payload over 154 characters is **rejected** rather than silently printed too large.

## Status

Written but **not yet compiled or run** — no TC22 available at the time of writing, and the build was not executed. Resource references, `R.*` usages, and every ViewBinding field have been cross-checked against the layouts statically, but that is not a substitute for a compiler.

Expect to fix build errors on first `assembleDebug`.
