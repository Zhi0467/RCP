// The same download-on-play player serves the panel and inline shell.
const videoPreview = document.getElementById("videoPreview");
const videoPlay = document.getElementById("videoPlay");
let videoRequest = null;
let videoObjectUrl = null;
function releaseVideo() {
  videoRequest?.abort();
  videoRequest = null;
  const player = videoPreview.querySelector("video");
  if (player) {
    player.pause();
    player.removeAttribute("src");
    player.load();
    player.remove();
  }
  if (videoObjectUrl) URL.revokeObjectURL(videoObjectUrl);
  videoObjectUrl = null;
}
videoPlay.addEventListener("click", async () => {
  releaseVideo();
  const request = new AbortController();
  videoRequest = request;
  videoPlay.disabled = true;
  videoPreview.classList.remove("failed");
  try {
    const response = await fetch(videoContentUrl, {
      credentials: "same-origin",
      signal: request.signal,
    });
    if (!response.ok) throw new Error("Video fetch failed");
    const blob = await response.blob();
    if (request.signal.aborted) return;
    videoObjectUrl = URL.createObjectURL(blob);
    const player = document.createElement("video");
    player.controls = true;
    player.playsInline = true;
    player.src = videoObjectUrl;
    videoPlay.remove();
    videoPreview.append(player);
    player.play().catch(() => {});
  } catch {
    if (request.signal.aborted) return;
    releaseVideo();
    videoPreview.classList.add("failed");
    videoPlay.setAttribute("aria-label", "Retry video");
    videoPlay.querySelector("path").setAttribute("d", "M20 7v5h-5 M20 12a8 8 0 1 0-2 6");
  } finally {
    videoPlay.disabled = false;
    if (videoRequest === request) videoRequest = null;
  }
});
window.addEventListener("pagehide", () => {
  releaseVideo();
  videoPreview.replaceChildren(videoPlay);
});
