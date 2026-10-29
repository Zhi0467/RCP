import type { ReactNode } from "react";
import { OverflowMenu, type OverflowMenuItem } from "../ui/OverflowMenu";

export type ChatHeaderAction = OverflowMenuItem;

/** The chat header's one overflow menu: chat actions, then any inline controls. */
export function ChatHeaderMenu({
  actions,
  children,
}: {
  actions: readonly ChatHeaderAction[];
  children?: ReactNode;
}) {
  return (
    <OverflowMenu
      label="Chat menu"
      items={actions}
      className="chat-header-menu"
      listClassName="chat-header-menu-list"
    >
      {children}
    </OverflowMenu>
  );
}
