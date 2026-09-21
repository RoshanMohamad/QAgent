/**
 * Record / stop / export (CLAUDE.md section 10).
 *
 * Export writes a JSON file rather than posting to a server. The recorder has
 * just watched someone log in, so the session is sensitive by construction:
 * the developer should see what it captured before it leaves the machine.
 * `qagent record import` is what turns the file into a test.
 */

const recordButton = document.getElementById("record");
const exportButton = document.getElementById("export");
const stats = document.getElementById("stats");
const status = document.getElementById("status");

function render(state) {
  const recording = Boolean(state.recording);
  recordButton.textContent = recording ? "Stop recording" : "Record";
  recordButton.dataset.recording = String(recording);
  exportButton.disabled = recording || !state.actions;
  stats.textContent = recording
    ? `Recording: ${state.actions || 0} actions, ${state.requests || 0} requests.`
    : state.actions
      ? `Recorded ${state.actions} actions, ${state.requests || 0} requests.`
      : "Not recording.";
}

function refresh() {
  chrome.runtime.sendMessage({ type: "qagent:status" }, (state) => render(state || {}));
}

recordButton.addEventListener("click", async () => {
  const state = await chrome.runtime.sendMessage({ type: "qagent:status" });
  if (state && state.recording) {
    await chrome.runtime.sendMessage({ type: "qagent:stop" });
  } else {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    await chrome.runtime.sendMessage({ type: "qagent:start", url: tab ? tab.url : null });
  }
  refresh();
});

exportButton.addEventListener("click", async () => {
  const { session } = await chrome.runtime.sendMessage({ type: "qagent:export" });
  const blob = new Blob([JSON.stringify(session, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  await chrome.downloads?.download?.({ url, filename: "qagent-session.json" }).catch(() => {
    // The downloads permission is optional; opening the blob is the fallback.
    chrome.tabs.create({ url });
  });
  status.textContent = "Exported. Import with: qagent record import <file>";
});

refresh();
