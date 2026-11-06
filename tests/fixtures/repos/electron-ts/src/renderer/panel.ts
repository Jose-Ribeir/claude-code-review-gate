import { ipcRenderer } from "electron";
import { formatBytes } from "../main/util";

export function refreshPanel(cacheBytes: number): string {
  void ipcRenderer.invoke("app:open", "panel");
  return `cache: ${formatBytes(cacheBytes)}`;
}
