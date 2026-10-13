const liveFrame = document.getElementById("preview");
const liveNotice = document.getElementById("notice");
let liveTimer = null;
let liveBusy = false;
let liveFinished = false;
let liveReady = false;
let lastLive = null;
// A shell is bound to one artifact/version/target; a new shell starts cacheless.
let graphCache = null;
let graphIds = new Set();
let liveDelay = defaultLiveDelay;
function markGraphStale(stale) {
  liveFrame.dataset.graphStale = String(stale);
  liveFrame.style.outline = stale ? "3px solid #c43c39" : "";
  liveFrame.style.outlineOffset = "-3px";
}
async function refreshLive() {
  clearTimeout(liveTimer);
  if (document.hidden || liveBusy || !liveReady || liveFinished) return;
  liveBusy = true;
  try {
    const url = new URL(liveUrl, window.location.href);
    if (graphCache) url.searchParams.set("head", graphCache.head);
    const response = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) throw new Error("Live data unavailable");
    const value = await response.json();
    if (value.kind !== "rcp-live-data" || value.version !== 1 || !Array.isArray(value.snapshots))
      throw new Error("Invalid live data response");
    const graph = value.snapshots.find((snapshot) => snapshot.kind === "graph");
    if (graph) {
      if (graph.error) {
        markGraphStale(true);
      } else if (graph.unchanged === true) {
        if (!graphCache || graph.head !== graphCache.head)
          throw new Error("Graph cache unavailable");
        value.snapshots = value.snapshots.map((snapshot) => snapshot === graph ? graphCache : snapshot);
        markGraphStale(false);
      } else if (typeof graph.head === "string" && Array.isArray(graph.nodes) && Array.isArray(graph.edges)) {
        graphCache = graph;
        graphIds = new Set(graph.nodes.map((node) => node.id));
        markGraphStale(false);
      } else throw new Error("Invalid graph snapshot");
    }
    lastLive = value.static ? null : value;
    liveFinished = value.static === true || (!graph && value.final === true && value.complete === true);
    liveDelay = value.refresh_seconds * 1000;
    if (liveNotice)
      liveNotice.textContent = value.reason || (value.complete ? "" : "Live data incomplete");
    if (lastLive) liveFrame.contentWindow?.postMessage(lastLive, "*");
  } catch (error) {
    if (graphCache) markGraphStale(true);
    if (liveNotice) liveNotice.textContent = error.message;
  } finally {
    liveBusy = false;
    if (!document.hidden && !liveFinished) liveTimer = setTimeout(refreshLive, liveDelay);
  }
}
window.addEventListener("message", (event) => {
  if (event.source !== liveFrame.contentWindow) return;
  const value = event.data;
  if (value?.type === "rcp-open-node" && value.version === 1 &&
      typeof value.id === "string" && graphIds.has(value.id)) {
    window.parent.postMessage({type: "rcp-open-node", version: 1,
      artifact_id: liveArtifactId, node_id: value.id}, window.location.origin);
    return;
  }
  if (value?.type !== "rcp-live-ready" || value.version !== 1) return;
  liveReady = true;
  if (lastLive) liveFrame.contentWindow.postMessage(lastLive, "*");
  refreshLive();
});
document.addEventListener("visibilitychange", () => {
  clearTimeout(liveTimer);
  if (!document.hidden) refreshLive();
});
window.addEventListener("pagehide", () => clearTimeout(liveTimer));
