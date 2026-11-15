import { BrowserWindow, dialog, ipcMain } from "electron";
import path from "node:path";

ipcMain.handle("app:open", async (event, target: string) => {
  const frame = event.senderFrame;
  if (!frame || !frame.url.startsWith("file://")) {
    throw new Error(`untrusted sender: ${frame?.url ?? "unknown"}`);
  }
  await dialog.showOpenDialog({ defaultPath: path.join(target) });
});

ipcMain.handle("diag:open", async (_event, name: string) => {
  const win = new BrowserWindow({ width: 480, height: 320 });
  await win.loadFile(path.join(__dirname, "../renderer/diagnostics.html"));
  return win.id;
});
