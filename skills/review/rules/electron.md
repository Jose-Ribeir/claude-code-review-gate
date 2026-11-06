<!--
  Electron review rules for the review gate. New file for the Kimchi port
  (not part of the upstream ocr corpus): sourced from the official Electron
  security checklist (electronjs.org/docs/latest/tutorial/security) and
  Electron IPC contract discipline. Sourced from public documentation;
  Apache-2.0, same license as the rest of this repository.
-->

# Electron-specific review rules

Apply these in addition to the default rubric (and the TypeScript rules) when
reviewing files that reference `electron` — main, preload, or renderer process
code. This file is appended to the rubric automatically when any reviewed file
imports or requires `electron` (see SKILL.md §2). Severity reflects Electron's
threat model: a compromised renderer is remote code execution, not just XSS.

Each item is a numbered, severity-tagged check with a one-line "what to look
for in a diff" hint.

1. **`contextIsolation: false`** — Security / high
   Diff: any `webPreferences` that adds, keeps, or re-enables `contextIsolation: false`; the safe default (true since Electron 12) must not be disabled.

2. **`nodeIntegration: true`** — Security / high
   Diff: `nodeIntegration`, `nodeIntegrationInWorker`, or `nodeIntegrationInSubFrames` set to true in any `webPreferences`; renderer must never get direct Node APIs.

3. **`sandbox: false`** — Security / high
   Diff: `sandbox: false` in `webPreferences` for windows loading remote or untrusted content; un-sandboxed renderers can reach IPC and Node-adjacent surfaces.

4. **`webSecurity` / mixed content disabled** — Security / high
   Diff: `webSecurity: false`, `allowRunningInsecureContent: true`, or `disableWebSecurity` anywhere; these switch off same-origin policy and HTTPS enforcement.

5. **Remote content loaded without isolation** — Security / high
   Diff: `loadURL('http://…')` or any non-`file://`/non-app-origin URL in a window whose preferences are not fully hardened (items 1–4 apply doubly to remote content).

6. **Missing `will-navigate` guard** — Security / high
   Diff: a `BrowserWindow`/`webContents` is created or its load target changed, but no `will-navigate` handler denies navigation away from the app origin (or an existing handler is removed/weakened).

7. **Missing `setWindowOpenHandler`** — Security / high
   Diff: new windows or webviews become possible (navigation, anchors, `window.open`) while no `setWindowOpenHandler` exists, or an existing one now returns `allow`/`action: 'allow'` unconditionally.

8. **`<webview>` tag usage** — Security / high
   Diff: a new `<webview>` or changes to its attributes (`src`, `webpreferences`, `preload`, `allowpopups`); webviews are the least-sandboxed surface — flag any `webpreferences` string and remote `src` without explicit justification.

9. **IPC channel pairing** — Correctness / high
   Diff: a channel literal changes on one side only — `ipcRenderer.invoke/send/sendSync('ch')` whose `ipcMain.handle/on/once('ch')` was renamed or removed (or vice versa); the Electron analog of a broken cross-file caller.

10. **IPC sender validation** — Security / high
    Diff: a new/changed `ipcMain.handle/on` callback that performs privileged work without validating the sender (`event.senderFrame.url` against an origin allowlist, or `event.sender`/`webContents` identity); `senderFrame` checks removed from an existing handler are the highest-signal case.

11. **IPC payload validation on the main side** — Security / high
    Diff: a handler that trusts `event.payload` shape (destructures, spreads into queries, passes to `fs`/`exec`/privileged APIs) without schema/explicit checks; renderer input is attacker-controlled by definition.

12. **Preload exposure surface** — Security / medium
    Diff: `contextBridge.exposeInMainWorld` gains keys beyond the minimal API the renderer needs, especially objects, functions with many args, or anything returning raw promises of privileged data; every exposed key is renderer-reachable attack surface.

13. **Raw `ipcRenderer` leaked through the bridge** — Security / high
    Diff: preload exposing `ipcRenderer` itself (or `ipcRenderer.on` with pass-through callbacks, or forwarding `IpcRendererEvent`) via `contextBridge`; this defeats contextIsolation — only narrow, named wrappers may cross.

14. **`shell.openExternal` on dynamic data** — Security / high
    Diff: `shell.openExternal(...)` called with anything but a hardcoded/validated literal; unvalidated URLs can open `file://`, smb, or arbitrary scheme handlers. Also `shell.showItemInFolder`/`openPath` on unvalidated paths.

15. **`child_process` / native modules with untrusted args** — Security / high
    Diff: `exec`/`execFile`/`spawn` (main process) or native modules whose arguments derive from renderer/IPC input; `exec` with any interpolation is an automatic high.

16. **Protocol handler registration** — Security / medium
    Diff: `protocol.registerFileProtocol`/`registerSchemesAsPrivileged`/`handle` additions — look for path traversal in the resolver (`..` not normalized, user input joined into a `file://` root) and for newly privileged schemes (fetch, cors, stream).

17. **`remote` module** — Security / high
    Diff: `enableRemoteModule: true` (legacy) or new `@electron/remote` imports; the remote module exposes main-process objects to the renderer and must not be (re)introduced.

18. **Permission request handlers** — Security / medium
    Diff: `setPermissionRequestHandler` added that returns `true`/grants for broad classes (geolocation, media, notifications, midi, clipboard) without user gesture; or an existing restrictive handler deleted while web APIs stay in use.

19. **Credentials/cookies in the renderer** — Security / high
    Diff: tokens, cookies, or secrets newly stored/read in renderer code (`localStorage`, `document.cookie`, constants in renderer bundles) or shipped to the renderer via IPC without need; secrets belong in the main process.

20. **DevTools in production** — Security / medium
    Diff: `webContents.openDevTools()` or `--remote-debugging-port` wiring added outside a dev flag; combined with un-sandboxed renderers this is a debugger-shaped hole.

21. **CSP missing or weakened** — Security / medium
    Diff: `Content-Security-Policy` removed, or its directives loosened (`unsafe-inline`, `unsafe-eval`, `*` origins, added `http:` sources) for windows that render remote or markdown-derived content; renderer XSS in Electron is RCE-shaped.

22. **Update integrity** — Security / high
    Diff: `electron-updater`/`autoUpdater` configuration changes — signature verification disabled (`verifyUpdateCodeSignature`, `disableWebInstaller`, custom unsigned feeds, `allowDowngrade: true`); an unsigned update channel is arbitrary code delivery.

23. **`enableBlinkFeatures` / `experimentalFeatures`** — Security / medium
    Diff: `webPreferences` enabling experimental Chromium features; these often ship with weakened security defaults and are not supported in production.

Severity elevation rule: for any renderer-process finding whose sink would
execute with Node/main privileges (innerHTML sinks reached from IPC data,
`eval`, dynamic imports in preload), raise the severity one step above what the
plain TypeScript rules would assign — the same defect class is worth more in an
Electron renderer than on a web page.
