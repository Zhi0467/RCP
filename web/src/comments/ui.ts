import type { CommentAnchor, CommentThread } from "./types.ts";
import { bulletinTime } from "../chat/bulletinModel.ts";
import { anchorQuote as quote, railState } from "./model.ts";
import {
  commentMentionCandidates,
  encodeCommentMentions,
  decodeCommentMentions,
} from "./mentions.ts";
import type { BulletinMember } from "../chat/bulletinModel.ts";
import type { Point } from "./box.ts";
import "./comments.css";

export interface CommentsOptions {
  maxSelections?: number;
  canPost: boolean;
  canSend: boolean;
  members: BulletinMember[];
  load: () => Promise<CommentThread[]>;
  create: (anchor: CommentAnchor, text: string) => Promise<CommentThread>;
  reply: (threadId: string, text: string) => Promise<unknown>;
  resolve: (threadId: string, resolved: boolean) => Promise<unknown>;
  send: (threadIds: string[]) => Promise<unknown>;
  locate?: (thread: CommentThread) => void;
  position?: (thread: CommentThread) => Point | null;
  pinRoot?: HTMLElement;
}

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", text = "") {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}

function button(label: string, action: () => void, className = "") {
  const node = element("button", className, label);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

export function mountComments(root: HTMLElement, options: CommentsOptions) {
  const abort = new AbortController();
  let threads: CommentThread[] = [];
  let selected: string | null = null;
  let busy = false;
  let disposed = false;
  let revision = 0;
  let frame = 0;
  let pending: CommentAnchor | null = null;
  const ticked = new Set<string>();
  const drafts = new Map<string, string>();
  const rail = element("aside", "rcp-comments");
  rail.setAttribute("aria-label", "Comments");
  const header = element("header", "rcp-comments-header");
  const list = element("div", "rcp-comments-list");
  const resolved = element("details", "rcp-comments-resolved");
  const summary = element("summary");
  const closedList = element("div");
  resolved.append(summary, closedList);
  const footer = element("footer", "rcp-comments-footer");
  const notice = element("div", "rcp-comments-notice");
  notice.setAttribute("role", "status");
  const pinLayer = element("div", "rcp-comment-pins");
  const composer = element("section", "rcp-comment-composer");
  composer.setAttribute("role", "dialog");
  composer.setAttribute("aria-label", "Comment");
  composer.hidden = true;
  header.append(
    element("strong", "", "Comments"),
    button("Comment", () => offer({ kind: "whole", comment: "" })),
  );
  rail.append(header, list, resolved, footer, notice);
  root.append(rail, composer);
  (options.pinRoot ?? document.body).append(pinLayer);

  function candidates() {
    return commentMentionCandidates(options.members, false);
  }
  function showError(error: unknown) {
    notice.textContent = error instanceof Error ? error.message : String(error);
  }
  async function run(action: () => Promise<unknown>) {
    if (busy) return;
    busy = true;
    notice.textContent = "";
    rail.setAttribute("aria-busy", "true");
    updateFooter();
    try {
      await action();
    } catch (error) {
      showError(error);
    } finally {
      await refresh();
      busy = false;
      rail.removeAttribute("aria-busy");
      render();
    }
  }
  async function refresh() {
    const request = ++revision;
    try {
      const next = await options.load();
      if (disposed || request !== revision) return;
      const changed = JSON.stringify(next) !== JSON.stringify(threads);
      threads = next;
      for (const id of ticked)
        if (
          !threads.some(
            (thread) => thread.thread_id === id && thread.state === "open" && thread.can_send,
          )
        )
          ticked.delete(id);
      if (changed || !list.childElementCount) render();
      else updateFooter();
      updatePins();
    } catch (error) {
      if (!disposed) showError(error);
    }
  }
  function locate(threadId: string) {
    const thread = threads.find((item) => item.thread_id === threadId);
    if (!thread) return;
    selected = threadId;
    if (thread.state === "resolved") resolved.open = true;
    for (const card of rail.querySelectorAll<HTMLElement>("[data-thread-id]")) {
      const active = card.dataset.threadId === threadId;
      card.classList.toggle("selected", active);
      if (active) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    options.locate?.(thread);
    updatePins();
  }
  function updatePins() {
    pinLayer.replaceChildren();
    threads
      .filter((thread) => thread.state === "open")
      .forEach((thread, index) => {
        const point = options.position?.(thread);
        const card = [...rail.querySelectorAll<HTMLElement>("[data-thread-id]")].find(
          (item) => item.dataset.threadId === thread.thread_id,
        );
        if (card)
          card.title =
            options.position && !point && thread.anchor.kind !== "whole"
              ? "The anchor is gone"
              : "";
        if (!point) return;
        const pin = button(String(index + 1), () => locate(thread.thread_id), "rcp-comment-pin");
        pin.setAttribute("aria-label", `Comment ${index + 1}`);
        pin.classList.toggle("selected", selected === thread.thread_id);
        Object.assign(pin.style, { left: `${point.x}px`, top: `${point.y}px` });
        pinLayer.append(pin);
      });
  }
  function updateFooter() {
    footer.replaceChildren();
    if (!options.canSend) return;
    const {
      selected: chosen,
      sendLength: length,
      canSend,
    } = railState(threads, ticked, "", options.maxSelections);
    const send = button(
      `Send ${chosen.length} to agent`,
      () => void run(() => options.send(chosen.map((thread) => thread.thread_id))),
      "rcp-comment-primary",
    );
    send.disabled = busy || !canSend;
    if (length > 32000) send.title = "The selected threads exceed 32,000 characters";
    else if (!canSend && chosen.length) send.title = "Too many selected threads";
    footer.append(send);
  }
  function editor(
    key: string,
    submit: (text: string, send: boolean) => Promise<unknown>,
    canSend: boolean,
  ) {
    const form = element("form", "rcp-comment-editor");
    const input = element("textarea");
    input.maxLength = 2048;
    input.placeholder = key === "composer" ? "Comment" : "Reply";
    input.setAttribute("aria-label", input.placeholder);
    input.dataset.draftKey = key;
    input.value = drafts.get(key) ?? "";
    const picker = element("div", "rcp-comment-picker");
    picker.setAttribute("role", "listbox");
    picker.hidden = true;
    const actions = element("div", "rcp-comment-actions");
    const save = button(key === "composer" ? "Comment" : "Reply", () => void savePost(false));
    const send = button("Send to agent", () => void savePost(true), "rcp-comment-primary");
    const sync = () => {
      save.disabled = busy || !input.value.trim();
      send.disabled = save.disabled;
    };
    async function savePost(toAgent: boolean) {
      if (!input.value.trim() || busy || !options.canPost || (toAgent && !options.canSend)) return;
      await run(async () => {
        await submit(encodeCommentMentions(input.value.trim(), candidates()), toAgent);
        drafts.delete(key);
        input.value = "";
        sync();
      });
    }
    input.addEventListener("input", () => {
      drafts.set(key, input.value);
      sync();
      const before = input.value.slice(0, input.selectionStart);
      const trigger = /(?:^|\s)@([^@\n]*)$/.exec(before);
      picker.replaceChildren();
      picker.hidden = !trigger;
      if (!trigger) return;
      const at = before.lastIndexOf("@");
      const pick = (label: string, agent: boolean) => {
        input.setRangeText(agent ? "" : `@${label} `, at, input.selectionStart, "end");
        drafts.set(key, input.value);
        picker.hidden = true;
        input.focus();
        sync();
        if (agent) void savePost(true);
      };
      if (canSend) picker.append(button("The target's agent", () => pick("", true)));
      for (const candidate of candidates().filter((item) =>
        item.label.toLowerCase().includes(trigger[1].toLowerCase()),
      )) {
        picker.append(button(candidate.label, () => pick(candidate.label, false)));
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        picker.hidden = true;
        event.stopPropagation();
      }
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void savePost(false);
    });
    actions.append(save);
    if (canSend) actions.append(send);
    form.append(input, picker, actions);
    sync();
    return form;
  }
  function card(thread: CommentThread) {
    const node = element("article", "rcp-comment-thread");
    node.dataset.threadId = thread.thread_id;
    node.addEventListener("click", (event) => {
      if (
        event.target instanceof Element &&
        !event.target.closest("button,input,textarea,select,a")
      )
        locate(thread.thread_id);
    });
    node.classList.toggle("selected", selected === thread.thread_id);
    const top = element("div", "rcp-comment-thread-top");
    if (thread.state === "open" && options.canSend && thread.can_send) {
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.setAttribute("aria-label", "Select thread");
      checkbox.checked = ticked.has(thread.thread_id);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) ticked.add(thread.thread_id);
        else ticked.delete(thread.thread_id);
        updateFooter();
      });
      top.append(checkbox);
    }
    const quoted = quote(thread.anchor);
    top.append(
      button(quoted || "Whole target", () => locate(thread.thread_id), "rcp-comment-quote"),
    );
    node.append(top);
    for (const post of thread.posts) {
      const row = element("section", "rcp-comment-post");
      const author = post.author.display_name || post.author.user_id || "Member";
      const byline = element("div", "rcp-comment-byline");
      const avatar = element("span", "rcp-comment-avatar", author.slice(0, 1).toUpperCase());
      avatar.setAttribute("aria-hidden", "true");
      const time = element("time", "", bulletinTime(post.created_at));
      time.dateTime = post.created_at;
      time.title = new Date(post.created_at).toLocaleString();
      byline.append(avatar, element("strong", "", author), time);
      row.append(
        byline,
        element(
          "p",
          "",
          decodeCommentMentions(
            post.text,
            commentMentionCandidates(
              [
                ...options.members,
                ...(post.mentions ?? []).map((mention) => ({
                  user_id: mention.id,
                  display_name: mention.display_name,
                })),
              ].filter(
                (member, index, members) =>
                  members.findIndex((item) => item.user_id === member.user_id) === index,
              ),
              false,
            ),
          ),
        ),
      );
      node.append(row);
    }
    if (thread.sent_operation_id) node.append(element("span", "rcp-comment-sent", "Sent to agent"));
    if (options.canPost) {
      node.append(
        editor(
          thread.thread_id,
          async (text, send) => {
            await options.reply(thread.thread_id, text);
            drafts.delete(thread.thread_id);
            if (send) await options.send([thread.thread_id]);
          },
          options.canSend && thread.can_send,
        ),
      );
      const actions = element("div", "rcp-comment-actions");
      actions.append(
        button(
          thread.state === "open" ? "Resolve" : "Reopen",
          () => void run(() => options.resolve(thread.thread_id, thread.state === "open")),
        ),
      );
      if (thread.state === "open" && options.canSend && thread.can_send)
        actions.append(
          button("Send thread to agent", () => void run(() => options.send([thread.thread_id]))),
        );
      node.append(actions);
    }
    return node;
  }
  function render() {
    const focused =
      document.activeElement instanceof HTMLTextAreaElement && rail.contains(document.activeElement)
        ? document.activeElement
        : null;
    const cursor = focused ? [focused.selectionStart, focused.selectionEnd] : null;
    const key = focused?.dataset.draftKey;
    list.replaceChildren(...threads.filter((thread) => thread.state === "open").map(card));
    const closed = threads.filter((thread) => thread.state === "resolved");
    summary.textContent = `Resolved · ${closed.length}`;
    resolved.hidden = !closed.length;
    closedList.replaceChildren(...closed.map(card));
    header.querySelector("button")!.hidden = !options.canPost;
    updateFooter();
    if (key && cursor) {
      const input = [...rail.querySelectorAll("textarea")].find(
        (item) => item.dataset.draftKey === key,
      );
      input?.focus({ preventScroll: true });
      input?.setSelectionRange(cursor[0], cursor[1]);
    }
  }
  function offer(anchor: CommentAnchor | null, point?: Point, initialText?: string) {
    if (!options.canPost) return;
    pending = anchor;
    if (initialText !== undefined) drafts.set("composer", initialText);
    composer.hidden = !anchor;
    composer.replaceChildren();
    if (!anchor) return;
    const actions = element("div", "rcp-comment-actions");
    actions.append(button("Cancel", () => offer(null)));
    composer.append(
      element("blockquote", "", quote(anchor)),
      editor(
        "composer",
        async (text, send) => {
          if (!pending) return;
          const thread = await options.create(pending, text);
          drafts.delete("composer");
          offer(null);
          if (send) await options.send([thread.thread_id]);
        },
        options.canSend,
      ),
      actions,
    );
    Object.assign(composer.style, {
      left: `${Math.max(8, Math.min(point?.x ?? window.innerWidth - 352, window.innerWidth - 352))}px`,
      top: `${Math.max(8, Math.min(point?.y ?? 80, window.innerHeight - 280))}px`,
    });
    composer.querySelector("textarea")?.focus();
  }
  const reposition = () => {
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(updatePins);
  };
  window.addEventListener("resize", reposition, { signal: abort.signal });
  document.addEventListener("scroll", reposition, { capture: true, signal: abort.signal });
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Escape") offer(null);
    },
    { signal: abort.signal },
  );
  render();
  void refresh();
  return {
    refresh,
    offer,
    locate,
    showError,
    update(next: Partial<Pick<CommentsOptions, "canPost" | "canSend" | "members">>) {
      if (
        Object.entries(next).every(
          ([key, value]) =>
            JSON.stringify(options[key as keyof CommentsOptions]) === JSON.stringify(value),
        )
      )
        return;
      Object.assign(options, next);
      render();
      updatePins();
      if (!options.canPost) {
        pending = null;
        composer.hidden = true;
      } else if (pending && !composer.hidden) {
        const bounds = composer.getBoundingClientRect();
        offer(pending, { x: bounds.x, y: bounds.y });
      }
    },
    destroy() {
      disposed = true;
      abort.abort();
      cancelAnimationFrame(frame);
      rail.remove();
      composer.remove();
      pinLayer.remove();
    },
  };
}
