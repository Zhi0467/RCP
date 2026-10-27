import { useReducer, type HTMLAttributes } from "react";
import { X } from "lucide-react";
import { dismissBanner, isBannerDismissed } from "./bannerDismissal";

interface Props extends HTMLAttributes<HTMLDivElement> {
  /** Names this occurrence; dismissing hides it until the key changes. */
  dismissKey: string;
}

/** Every banner: its own frame and content, one dismiss control and store. */
export function Banner({ dismissKey, className, children, ...rest }: Props) {
  const [, rerender] = useReducer((value: number) => value + 1, 0);
  if (isBannerDismissed(dismissKey)) return null;
  return (
    <div {...rest} className={className ? `banner ${className}` : "banner"}>
      {children}
      <button
        className="banner-dismiss"
        type="button"
        aria-label="Dismiss"
        onClick={() => {
          dismissBanner(dismissKey);
          rerender();
        }}
      >
        <X size={14} aria-hidden="true" />
      </button>
    </div>
  );
}
