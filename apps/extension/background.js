/**
 * Holds the recording and captures network traffic (CLAUDE.md section 10).
 *
 * The service worker owns the session rather than the content script for a
 * reason that is easy to miss: a recorded flow almost always navigates, and a
 * content script is destroyed on every navigation. State kept there would lose
 * everything before the checkout page.
 *
 * Network requests come from `chrome.webRequest`, which sees XHR/fetch the
 * page makes. That is what turns a UI recording into an API test: the clicks
 * say what the user did, the requests say what the application did about it.
 */

const session = {
  recording: false,
  startedAt: null,
  startUrl: null,
  actions: [],
  requests: [],
};

/** A recorded session is held in memory and capped. An unbounded recorder on a
 *  chatty single-page app will happily consume a gigabyte of polling traffic. */
const MAX_ACTIONS = 500;
const MAX_REQUESTS = 1000;

/** Request headers never worth recording: they are credentials, and a recorded
 *  session is exported to a file and pasted into issues. */
const SENSITIVE_HEADERS = new Set([
  "authorization",
  "cookie",
  "set-cookie",
  "x-api-key",
  "x-auth-token",
  "proxy-authorization",
]);

function reset(startUrl) {
  session.recording = true;
  session.startedAt = Date.now();
  session.startUrl = startUrl || null;
  session.actions = [];
  session.requests = [];
}

function scrubHeaders(headers) {
  const out = {};
  for (const header of headers || []) {
    const name = String(header.name || "").toLowerCase();
    out[name] = SENSITIVE_HEADERS.has(name) ? "[redacted]" : String(header.value || "").slice(0, 200);
  }
  return out;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message.type) {
    case "qagent:hello":
      sendResponse({ recording: session.recording });
      return true;

    case "qagent:action":
      if (session.recording && session.actions.length < MAX_ACTIONS) {
        session.actions.push(message.action);
      }
      sendResponse({ ok: true });
      return true;

    case "qagent:start":
      reset(message.url);
      broadcast(true);
      sendResponse({ ok: true, recording: true });
      return true;

    case "qagent:stop":
      session.recording = false;
      broadcast(false);
      sendResponse({ ok: true, session: exportSession() });
      return true;

    case "qagent:status":
      sendResponse({
        recording: session.recording,
        actions: session.actions.length,
        requests: session.requests.length,
      });
      return true;

    case "qagent:export":
      sendResponse({ session: exportSession() });
      return true;

    default:
      return false;
  }
});

/** Tell every tab whether to record, so a flow spanning several tabs or a
 *  navigation mid-session keeps capturing. */
function broadcast(recording) {
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs) {
      if (!tab.id) continue;
      chrome.tabs.sendMessage(tab.id, { type: "qagent:set-recording", recording }, () => {
        // A tab with no content script (chrome://, the web store) rejects this.
        // Reading lastError is what stops it being logged as an unhandled error.
        void chrome.runtime.lastError;
      });
    }
  });
}

/**
 * Request bodies, which is what makes a recorded POST replayable at all.
 *
 * Without one, a generated check replays the request empty, the application
 * correctly answers 422, and the check fails on every run while nothing is
 * wrong - a false positive, which the importer refuses to generate
 * (modules/recorder/session.py). Capturing the body is what lets it assert
 * something useful instead.
 *
 * Only JSON is kept. A multipart upload cannot be replayed by the declarative
 * runner, and recording it would produce a check that sends the wrong content
 * type. Secret-looking fields are redacted again server side; doing it in both
 * places means a session file is safe to read even if this half is bypassed.
 */
const pendingBodies = new Map();

chrome.webRequest.onBeforeRequest.addListener(
  (details) => {
    if (!session.recording) return;
    if (!["xmlhttprequest", "fetch"].includes(details.type)) return;

    const raw = details.requestBody && details.requestBody.raw;
    if (!raw || !raw.length || !raw[0].bytes) return;

    try {
      const text = new TextDecoder("utf-8").decode(raw[0].bytes);
      if (text.length > 20000) return;
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === "object") pendingBodies.set(details.requestId, parsed);
    } catch {
      // Not JSON (form data, a file upload, a protobuf). Deliberately dropped.
    }
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["requestBody"],
);

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!session.recording) return;
    if (session.requests.length >= MAX_REQUESTS) return;
    // Only the application's own API traffic is interesting; documents, images
    // and stylesheets are noise a generated test would never assert on.
    if (!["xmlhttprequest", "fetch", "ping"].includes(details.type)) return;

    session.requests.push({
      id: details.requestId,
      method: details.method,
      url: details.url,
      type: details.type,
      headers: scrubHeaders(details.requestHeaders),
      body: pendingBodies.get(details.requestId) ?? null,
      at: details.timeStamp,
    });
    pendingBodies.delete(details.requestId);
  },
  { urls: ["http://*/*", "https://*/*"] },
  ["requestHeaders", "extraHeaders"],
);

chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (!session.recording) return;
    const request = session.requests.find((r) => r.id === details.requestId);
    if (request) {
      request.status = details.statusCode;
      request.completedAt = details.timeStamp;
    }
    pendingBodies.delete(details.requestId);
  },
  { urls: ["http://*/*", "https://*/*"] },
);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => {
    if (!session.recording) return;
    const request = session.requests.find((r) => r.id === details.requestId);
    if (request) request.error = details.error;
    // Cleared on every terminal outcome: a page that fires hundreds of aborted
    // requests would otherwise leak an entry per request for the whole session.
    pendingBodies.delete(details.requestId);
  },
  { urls: ["http://*/*", "https://*/*"] },
);

function exportSession() {
  return {
    version: 1,
    started_at: session.startedAt,
    start_url: session.startUrl,
    // Two lists rather than one merged timeline, each already in the order it
    // was captured. Both carry timestamps, so the importer can correlate a
    // request with the click that caused it without this file taking a view.
    actions: session.actions,
    requests: session.requests.map(({ id, ...rest }) => rest),
  };
}
