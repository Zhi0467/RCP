import { resourceKeyId } from "../core/resourceStore";

/** Entity watermarks are project-wide; ref selects the payload route only. */
export function chatResourceKey(projectId: string, ref: string, chatId: string): string {
  return resourceKeyId({
    backend: "",
    member: "",
    project: projectId,
    name: "chat",
    entity: chatId,
    query: ref,
  });
}
