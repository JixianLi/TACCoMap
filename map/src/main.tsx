import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@core/index.css";
import App from "@core/App";
import { COLLABORATION_MAP_CONFIG } from "@/Scenarios/CollaborationMap";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App initialConfig={COLLABORATION_MAP_CONFIG} />
  </StrictMode>
);
