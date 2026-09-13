import { SunMoon } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import {
  COLOR_MODE_CHOICES,
  THEME_CHOICES,
  colorModeChoiceLabel,
  themeChoiceLabel,
  type ColorModeChoice,
  type ThemeChoice,
} from "../theme";
import "./AppearancePicker.css";

export interface AppearancePickerProps {
  themeChoice: ThemeChoice;
  colorModeChoice: ColorModeChoice;
  onThemeChoiceChange: (theme: ThemeChoice) => void;
  onColorModeChoiceChange: (mode: ColorModeChoice) => void;
}

export function AppearancePicker({
  themeChoice,
  colorModeChoice,
  onThemeChoiceChange,
  onColorModeChoiceChange,
}: AppearancePickerProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panelId = useId();

  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]')?.focus();
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div className="appearance-picker" ref={root}>
      <button
        className="button secondary appearance-picker-trigger"
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((current) => !current)}
        ref={trigger}
      >
        <SunMoon size={15} aria-hidden="true" /> Display
      </button>
      {open && (
        <section className="appearance-picker-panel" aria-label="Display" id={panelId}>
          <div className="appearance-picker-row">
            <span>Mode</span>
            <div className="appearance-picker-options" role="group" aria-label="Color mode">
              {COLOR_MODE_CHOICES.map((choice) => (
                <button
                  type="button"
                  aria-pressed={choice === colorModeChoice}
                  onClick={() => onColorModeChoiceChange(choice)}
                  key={choice}
                >
                  {colorModeChoiceLabel(choice)}
                </button>
              ))}
            </div>
          </div>
          <div className="appearance-picker-row">
            <span>Theme</span>
            <div className="appearance-picker-options" role="group" aria-label="Theme">
              {THEME_CHOICES.map((choice) => (
                <button
                  type="button"
                  aria-pressed={choice === themeChoice}
                  onClick={() => onThemeChoiceChange(choice)}
                  key={choice}
                >
                  {themeChoiceLabel(choice)}
                </button>
              ))}
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
