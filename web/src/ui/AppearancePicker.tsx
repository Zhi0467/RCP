import { Minus, Plus } from "lucide-react";
import {
  COLOR_MODE_CHOICES,
  THEME_CHOICES,
  colorModeChoiceLabel,
  themeChoiceLabel,
  type ColorModeChoice,
  type ThemeChoice,
} from "./theme";
import { TEXT_SCALE_MAX, TEXT_SCALE_MIN, type TextScaleAction } from "./textScale";
import "../components/AppearancePicker.css";

export interface AppearancePickerProps {
  themeChoice: ThemeChoice;
  colorModeChoice: ColorModeChoice;
  onThemeChoiceChange: (theme: ThemeChoice) => void;
  onColorModeChoiceChange: (mode: ColorModeChoice) => void;
}

/** Text size is a desktop webview zoom, so only the desktop app offers it. */
export interface TextScaleControl {
  value: number;
  onChange: (action: TextScaleAction) => void;
}

/** Display preferences, shown inside the identity menu. */
export function AppearancePicker({
  themeChoice,
  colorModeChoice,
  onThemeChoiceChange,
  onColorModeChoiceChange,
  textScale,
}: AppearancePickerProps & { textScale?: TextScaleControl }) {
  return (
    <section className="appearance-picker" aria-label="Display">
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
      {textScale && (
        <div className="appearance-picker-row">
          <span>Text size</span>
          <div className="text-scale-controls" role="group" aria-label="Interface text size">
            <button
              className="icon-button"
              type="button"
              disabled={textScale.value <= TEXT_SCALE_MIN}
              onClick={() => textScale.onChange("decrease")}
              aria-label="Decrease text size"
            >
              <Minus size={16} />
            </button>
            <button
              className="text-scale-value"
              type="button"
              onClick={() => textScale.onChange("reset")}
              aria-label="Reset text size to 100 percent"
            >
              {textScale.value}%
            </button>
            <button
              className="icon-button"
              type="button"
              disabled={textScale.value >= TEXT_SCALE_MAX}
              onClick={() => textScale.onChange("increase")}
              aria-label="Increase text size"
            >
              <Plus size={16} />
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
