/**
 * The one clipboard write. The async Clipboard API exists only in secure
 * contexts, and a backend reached over plain HTTP on a private network is not
 * one; there the selection-based copy still works inside the tap's user gesture.
 */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  area.style.top = "0";
  area.style.left = "0";
  document.body.append(area);
  try {
    area.select();
    area.setSelectionRange(0, text.length);
    if (!document.execCommand("copy")) throw new Error("Copy was refused.");
  } finally {
    area.remove();
  }
}
