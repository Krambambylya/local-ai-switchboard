import { PanelLeft, PanelRight, X } from "lucide-react";
import { useEffect, useState } from "react";

import { ChatWorkspace } from "../chat/ChatWorkspace";
import { IconButton } from "../ui/IconButton";
import { SystemOverview } from "../system/SystemOverview";
import { NavPanel } from "./NavPanel";
import styles from "./AppShell.module.css";

export function AppShell() {
  const [navOpen, setNavOpen] = useState(true);
  const [systemOpen, setSystemOpen] = useState(true);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 1100px)");
    const apply = () => {
      setNavOpen(media.matches);
      setSystemOpen(media.matches);
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, []);

  return (
    <div className={styles.shell} data-nav={navOpen ? "open" : "closed"} data-system={systemOpen ? "open" : "closed"}>
      <div className={styles.nav}>
        <NavPanel onClose={() => setNavOpen(false)} />
      </div>
      <div className={styles.main}>
        <header className={styles.toolbar}>
          <button type="button" className={styles.navToggle} aria-label="Show navigation" onClick={() => setNavOpen(true)}>
            <PanelLeft size={16} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={styles.tool}
            aria-label={systemOpen ? "Hide system overview" : "Show system overview"}
            aria-pressed={systemOpen}
            onClick={() => setSystemOpen((open) => !open)}
          >
            <PanelRight size={16} aria-hidden="true" />
          </button>
        </header>
        <ChatWorkspace />
      </div>
      <div className={styles.system}>
        <IconButton label="Close system overview" className={styles.close} onClick={() => setSystemOpen(false)}>
          <X size={16} aria-hidden="true" />
        </IconButton>
        <SystemOverview />
      </div>
    </div>
  );
}
