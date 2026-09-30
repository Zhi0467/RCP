const liveFrame = document.getElementById("preview");
const liveNotice = document.getElementById("notice");
let liveTimer = null;
let liveBusy = false;
let liveFinished = false;
let liveReady = false;
let lastLive = null;
let liveDelay = defaultLiveDelay;
async function refreshLive() {
  clearTimeout(liveTimer);
  if (document.hidden || liveBusy || !liveReady || liveFinished) return;
  liveBusy = true;
  try {
    const response = await fetch(liveUrl, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("Live data unavailable");
    const value = await response.json();
    if (value.kind !== "rcp-live-data" || value.version !== 1)
      throw new Error("Invalid live data response");
    lastLive = value.static ? null : value;
    liveFinished = value.static === true || (value.final === true && value.complete === true);
    liveDelay = value.refresh_seconds * 1000;
    if (liveNotice)
      liveNotice.textContent = value.reason || (value.complete ? "" : "Live data incomplete");
    if (lastLive) liveFrame.contentWindow?.postMessage(lastLive, "*");
  } catch (error) {
    if (liveNotice) liveNotice.textContent = error.message;
  } finally {
    liveBusy = false;
    if (!document.hidden && !liveFinished) liveTimer = setTimeout(refreshLive, liveDelay);
  }
}
window.addEventListener("message", (event) => {
  if (
    event.source !== liveFrame.contentWindow ||
    event.data?.type !== "rcp-live-ready" ||
    event.data.version !== 1
  )
    return;
  liveReady = true;
  if (lastLive) liveFrame.contentWindow.postMessage(lastLive, "*");
  refreshLive();
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(liveTimer);
  if (!document.hidden) refreshLive();
});
window.addEventListener("pagehide", () => clearTimeout(liveTimer));
