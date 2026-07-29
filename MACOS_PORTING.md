# AEye macOS Porting Guide

This branch is the handoff point for continuing the macOS port on a real Mac.
The current application remains Windows-only at runtime even though several
upstream Agent-S actions already contain basic `darwin` keyboard shortcuts.

## Recommended initial target

- Apple Silicon Mac
- macOS 14 or newer
- Python 3.11
- PySide6 and PyObjC
- Full-desktop mode first, locked-window mode second

Supporting one modern macOS/Apple Silicon configuration first avoids mixing
platform bugs with Intel, older macOS, and packaging differences.

## What is currently missing

### Platform abstraction

`gui_agents/s3/utils/window_target.py` is currently a Windows implementation.
It directly uses HWND values, `win32gui`, `win32api`, `win32con`, and
`win32process`. The UI and worker also call Windows-specific behavior directly.

Introduce a backend boundary such as:

```text
gui_agents/s3/platform/
├── base.py
├── factory.py
├── windows.py
└── macos.py
```

The base backend should provide:

- desktop geometry and screenshots;
- window enumeration and stable native identifiers;
- window geometry, PID, title, and minimized state;
- capture of a selected window;
- foreground-window detection and activation;
- application launch and application switching;
- point-to-window diagnostics;
- best-effort window raising or pinning;
- exclusion of AEye's decision overlay from screenshots.

Move the existing behavior into `windows.py` without changing its semantics
before implementing `macos.py`. Replace names such as `hwnd` with a neutral
`native_id` at the shared interface, while allowing the Windows backend to keep
HWND internally.

### Python dependencies

`setup.py` currently installs `paddleocr` and `paddlepaddle` unconditionally.
These packages may not have a compatible wheel for the selected Apple Silicon
Python/macOS combination and can block installation before the GUI is tested.

As part of Phase 1:

- identify which S3 paths actually require OCR;
- move heavyweight OCR packages into an optional extra such as `.[ocr]`;
- keep the core GUI/macOS backend installable without Paddle;
- retain Windows compatibility and document how to enable OCR explicitly.

### Desktop capture

`DesktopController.current_info()` currently calls Windows virtual-screen
metrics. A macOS backend must obtain the union of active display bounds and
capture it after Screen Recording permission is granted.

Preferred implementation:

- ScreenCaptureKit for production capture on modern macOS;
- Quartz display APIs for display geometry;
- a Pillow `ImageGrab` implementation only as an early compatibility fallback.

Verify multiple displays, displays positioned left or above the primary
display, and mixed Retina scale factors.

### Window enumeration and capture

Replace HWND enumeration with Quartz/CoreGraphics window information:

- `CGWindowListCopyWindowInfo` for visible window metadata;
- `CGWindowID` plus owner PID as the stable window identity;
- ScreenCaptureKit window filters for selected-window capture;
- Accessibility APIs where window state or activation is not available through
  CoreGraphics.

Decide whether locked-window screenshots include the complete decorated window
or only its content. macOS does not expose a universal Win32-style client area
for every third-party application. Using the complete window consistently is a
reasonable first implementation.

### Input and coordinate conversion

PyAutoGUI can provide an early prototype, but production input should use
Quartz `CGEvent` APIs for predictable mouse and keyboard behavior.

The implementation must explicitly handle:

- screenshot pixels versus Cocoa logical points;
- Retina scale factors;
- top-left image coordinates versus Quartz/Cocoa coordinate conventions;
- multiple-display origins;
- Unicode text through the clipboard;
- Command shortcuts instead of Control where appropriate;
- application switching with Command+Tab rather than Alt+Tab.

Create calibration tests before trusting model-generated coordinates. A click
near each corner and the center of every display/window should land within a
small, measured tolerance.

### Window activation and locked-window behavior

Windows currently pins a selected third-party window with `SetWindowPos` and
focuses it with `SetForegroundWindow`. macOS generally does not allow another
application to make arbitrary third-party windows permanently floating.

For the first macOS version:

- activate the owning application with `NSRunningApplication`/`NSWorkspace`;
- raise the selected window through Accessibility APIs;
- validate its PID/native ID immediately before input;
- document locked mode as "raise before each action" rather than guaranteed
  permanent topmost behavior.

### Application discovery, switching, and launching

Replace Windows Start-menu behavior with:

- `NSWorkspace.sharedWorkspace.runningApplications()` for running apps;
- `NSRunningApplication.activateWithOptions_()` for activation;
- `NSWorkspace.openApplicationAtURL_configuration_completionHandler_()` or an
  equivalent API for launching installed applications;
- bundle identifiers where available, with localized names as fallback.

Do not use Spotlight keystrokes as the primary launch path.

### Decision overlay

The floating decision panel currently uses Windows
`SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`. On macOS, obtain the native
`NSWindow` for the PySide window and use a supported capture-exclusion method.
Candidates include `NSWindowSharingNone` and explicitly excluding the AEye
window/application in ScreenCaptureKit content filters.

The overlay must remain:

- always visible to the user;
- non-activating;
- mouse/input transparent;
- absent from screenshots sent to the model;
- hidden when the task completes, fails, or is stopped.

Test the actual captured pixels. A successful API return alone is not enough.

### Secrets and environment variables

The current GUI reads `fyx_api_key` and `OPENROUTER_API_KEY` from the process
environment, with an additional Windows Registry fallback. macOS has no
equivalent fallback yet, and apps launched from Finder do not necessarily
inherit Terminal environment variables.

For an initial developer build, launch AEye from a configured Terminal. For a
packaged build, add macOS Keychain storage or an explicit local settings flow
that never writes secrets to Git or run logs.

### Permissions and user guidance

macOS requires explicit consent for:

- Screen Recording;
- Accessibility;
- Automation/Apple Events if used;
- possibly Input Monitoring, depending on the final input implementation.

Add a startup permission check with a clear message and a button/instruction to
open the relevant System Settings page. The application should fail safely and
avoid executing partial actions when permission is missing.

### Packaging and distribution

Development can run from a virtual environment. Distribution will additionally
need:

- an `.app` bundle (for example through PyInstaller);
- a stable bundle identifier;
- usage descriptions/entitlements where required;
- code signing;
- notarization and Gatekeeper testing;
- validation that permissions persist after upgrades.

## Suggested implementation phases

### Phase 1: backend extraction

1. Split platform-blocking optional dependencies from the core install.
2. Add the platform interface and factory.
3. Move existing Windows implementation behind it.
4. Remove direct `win32gui` imports from `gui_app.py`.
5. Keep all existing Windows tests passing.
6. Add fake-backend tests that run on any operating system.

### Phase 2: macOS full-desktop MVP

1. Implement display enumeration and full-desktop capture.
2. Implement Quartz mouse, keyboard, scrolling, and Unicode paste.
3. Implement running-app enumeration, activation, and launch.
4. Add Screen Recording and Accessibility permission checks.
5. Run simple tasks across two ordinary applications.

### Phase 3: locked-window mode

1. Enumerate windows and bind `CGWindowID + PID`.
2. Capture only the selected window.
3. Raise and validate it before every action.
4. Implement and test pixel/point coordinate mapping.
5. Test moved, resized, minimized, full-screen, and closed windows.

### Phase 4: overlay and packaging

1. Exclude the floating decision overlay from model screenshots.
2. Package AEye as an app bundle.
3. Add signing/notarization documentation.
4. Add a self-hosted Mac smoke-test workflow if a dedicated machine is
   available.

## macOS development setup

On the Mac:

```bash
xcode-select --install
git clone https://github.com/thiopheneche/AEye.git
cd AEye
git switch macos-support
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e '.[dev]'
```

If installation fails on Paddle/PaddleOCR, do not force an unrelated wheel or
disable macOS security features. Open the repository in Codex and complete the
Phase 1 optional-dependency split first; then recreate the virtual environment.

Then configure the API keys in the same Terminal session before launching the
developer build:

```bash
export fyx_api_key='...'
export OPENROUTER_API_KEY='...'
python -m gui_agents.s3.gui_app
```

Do not place real keys in source files, shell history committed to the repo, test
fixtures, screenshots, or logs.

## Acceptance checklist

- The GUI starts without importing any Win32-only package on macOS.
- Permission failures produce clear instructions and execute no input.
- Full-desktop screenshots have correct dimensions and display origins.
- Mouse clicks match screenshot coordinates on Retina and non-Retina displays.
- Command shortcuts and Unicode text input work.
- Running applications can be activated without opening duplicate instances.
- Locked-window capture follows a moved/resized window.
- Closing or reusing a native window ID stops the task safely.
- The decision overlay remains readable but is absent from model screenshots.
- Stop terminates immediately and restores the main window.
- Windows behavior and tests remain unchanged.

## Suggested first Codex task on the Mac

Use this request after opening the repository in Codex on the Mac:

> Read `MACOS_PORTING.md` completely. Start Phase 1 by separating Paddle/OCR
> from the core dependencies, introducing a platform backend interface, moving
> the existing Windows implementation behind it, and preserving all Windows
> behavior. Then implement the smallest macOS
> full-desktop backend that can enumerate displays, capture the desktop, and
> report missing Screen Recording/Accessibility permissions safely. Add tests,
> run them, and commit only to `macos-support`.
