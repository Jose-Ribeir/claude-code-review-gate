import { ipcRenderer } from "electron";

export interface DiagnosticsReport {
  startedAt: string;
  rendererVersion: string;
}

export function reportDiagnostics(report: DiagnosticsReport): void {
  ipcRenderer.send("channel-x", report);
}
