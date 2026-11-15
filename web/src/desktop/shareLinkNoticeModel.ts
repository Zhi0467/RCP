export function sshTargetForHost(host: string): string {
  const bracketed = /^\[([^\]]+)\](?::\d+)?$/.exec(host);
  if (bracketed) return bracketed[1];
  return host.replace(/:\d+$/, "");
}
