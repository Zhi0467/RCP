export function displayedVersion(contentUrl: string, baseUrl: string): string | null {
  return new URL(contentUrl, baseUrl).searchParams.get("version_id");
}
