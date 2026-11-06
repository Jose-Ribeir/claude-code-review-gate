import { BrowserWindow } from "electron";
import path from "node:path";

export function createSettingsWindow(): BrowserWindow {
  const win = new BrowserWindow({
    width: 520,
    height: 420,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });
  void win.loadFile(path.join(__dirname, "../renderer/settings.html"));
  return win;
}
