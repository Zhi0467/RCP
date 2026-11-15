import { useState, type ReactNode } from "react";
import { copyShareLink, shareLinkGraphTarget, useShareLinks } from "../core/shareLinks";
import type { GraphTargetRef } from "../core/types";
import { OverflowMenu, type OverflowMenuItem } from "../ui/OverflowMenu";

export type ChatHeaderAction = OverflowMenuItem;

/** The chat header's one overflow menu: chat actions, then any inline controls.
 *  In a team space, Copy link follows the first action. */
export function ChatHeaderMenu({
  actions,
  link,
  children,
}: {
  actions: readonly ChatHeaderAction[];
  link?: { projectId: string; chatId: string; graphTarget: GraphTargetRef };
  children?: ReactNode;
}) {
  const share = useShareLinks();
  const [notice, setNotice] = useState<string | null>(null);
  const items =
    share && link
      ? [
          ...actions.slice(0, 1),
          {
            label: "Copy link",
            onSelect: () => {
              setNotice(null);
              copyShareLink(link.projectId, {
                kind: "chat",
                item_id: link.chatId,
                graph_target: shareLinkGraphTarget(link.graphTarget),
              }).catch((cause: unknown) =>
                setNotice(cause instanceof Error ? cause.message : String(cause)),
              );
            },
          },
          ...actions.slice(1),
        ]
      : actions;
  return (
    <>
      <OverflowMenu
        label="Chat menu"
        items={items}
        className="chat-header-menu"
        listClassName="chat-header-menu-list"
      >
        {children}
      </OverflowMenu>
      {notice && <span role="alert">{notice}</span>}
    </>
  );
}
