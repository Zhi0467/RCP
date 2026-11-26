import { createRoot } from "react-dom/client";
import { Settings } from "lucide-react";
import { FeedbackButton } from "../../src/feedback/FeedbackButton";
import { ProjectMenu } from "../../src/projects/PhoneProjectChrome";
import "../../src/styles.css";

createRoot(document.getElementById("root")!).render(
  <header className="project-header" style={{ display: "flex", justifyContent: "flex-end" }}>
    <ProjectMenu>
      <button className="icon-button" aria-label="Space settings">
        <Settings size={16} />
      </button>
      <FeedbackButton className="icon-button feedback-control" />
    </ProjectMenu>
  </header>,
);
