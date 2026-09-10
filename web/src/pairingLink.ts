// A phone that scanned the Connect a device QR arrives with `#pair=CODE`. App
// normalizes the hash before the login screen mounts, so the code is read once
// at module load and handed to the login screen.

const PAIRING_CODE_PATTERN = /^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{6}$/;

export function pairingCodeFromHash(hash: string): string | null {
  const match = /^#pair=([^&]+)$/.exec(hash.trim());
  if (!match) return null;
  const code = decodeURIComponent(match[1]).trim().toUpperCase();
  return PAIRING_CODE_PATTERN.test(code) ? code : null;
}

export const initialPairingCode: string | null =
  typeof window === "undefined" ? null : pairingCodeFromHash(window.location.hash);
