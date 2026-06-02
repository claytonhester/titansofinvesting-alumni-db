"use client";

import { useState, type ReactNode } from "react";

export type TabKey = "overview" | "directory" | "news" | "build";

interface TabDef {
  key: TabKey;
  label: string;
  badge?: number;
}

interface TabsProps {
  defaultTab: TabKey;
  tabs: TabDef[];
  panels: Record<TabKey, ReactNode>;
}

export default function Tabs({ defaultTab, tabs, panels }: TabsProps) {
  const [active, setActive] = useState<TabKey>(defaultTab);

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Directory views">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={active === t.key}
            className={`tab${active === t.key ? " is-active" : ""}`}
            onClick={() => setActive(t.key)}
          >
            {t.label}
            {t.badge !== undefined && t.badge > 0 && (
              <span className="tab-badge">{t.badge.toLocaleString()}</span>
            )}
          </button>
        ))}
      </div>
      {tabs.map((t) => (
        <div key={t.key} role="tabpanel" hidden={active !== t.key}>
          {panels[t.key]}
        </div>
      ))}
    </>
  );
}
