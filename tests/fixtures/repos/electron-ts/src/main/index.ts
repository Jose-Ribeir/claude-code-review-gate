import { app, BrowserWindow } from "electron";
import path from "node:path";

function createMainWindow(): void {
  const win = new BrowserWindow({
    width: 960,
    height: 640,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  void win.loadFile(path.join(__dirname, "../renderer/panel.html"));
}

app.whenReady().then(() => {
  createMainWindow();
});
