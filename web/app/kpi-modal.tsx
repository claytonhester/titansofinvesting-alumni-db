"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import type { KpiMember } from "@/lib/db";
import type { SignatureStat } from "@/lib/insights";

interface KpiModalProps {
  stats: SignatureStat[];
  members: Record<string, KpiMember[]>;
  initialKey: string;
  onClose: () => void;
}

// Short tab labels (the scorecard labels are too long for a tab strip).
const TAB_LABEL: Record<string, string> = {
  buy_side: "Buy-side",
  reached_senior_leadership: "Senior leadership",
  founder_partner: "Founders",
  still_first_firm: "First firm",
  grad_degree: "Grad degree",
  years_to_senior_leadership: "Years to senior",
  tenure: "Tenure",
  left_texas: "Left Texas",
};

interface Bucket {
  min: number;
  max: number;
  label: string;
}

// Distribution KPIs draw a histogram instead of a ring. Buckets chosen to read
// cleanly for each metric's typical range.
const HISTOGRAM: Record<string, { buckets: Bucket[]; caption: string }> = {
  years_to_senior_leadership: {
    caption: "years from graduation to senior leadership",
    buckets: [
      { min: 0, max: 2, label: "0–2" },
      { min: 3, max: 5, label: "3–5" },
      { min: 6, max: 9, label: "6–9" },
      { min: 10, max: 14, label: "10–14" },
      { min: 15, max: Infinity, label: "15+" },
    ],
  },
  tenure: {
    caption: "years at current firm",
    buckets: [
      { min: 0, max: 1, label: "0–1" },
      { min: 2, max: 3, label: "2–3" },
      { min: 4, max: 6, label: "4–6" },
      { min: 7, max: 10, label: "7–10" },
      { min: 11, max: Infinity, label: "11+" },
    ],
  },
};

function Ring({ pct }: { pct: number }) {
  const r = 54;
  const circ = 2 * Math.PI * r;
  const dash = (Math.max(0, Math.min(100, pct)) / 100) * circ;
  return (
    <div className="kpi-graphic">
      <svg viewBox="0 0 140 140" className="kpi-ring" role="img" aria-label={`${pct}%`}>
        <circle cx="70" cy="70" r={r} className="kpi-ring-track" />
        <circle
          cx="70"
          cy="70"
          r={r}
          className="kpi-ring-fill"
          strokeDasharray={`${dash} ${circ}`}
          transform="rotate(-90 70 70)"
        />
        <text x="70" y="74" className="kpi-ring-pct" textAnchor="middle">
          {pct}%
        </text>
      </svg>
    </div>
  );
}

function Histogram({
  values,
  buckets,
  caption,
}: {
  values: number[];
  buckets: Bucket[];
  caption: string;
}) {
  const counts = buckets.map(
    (b) => values.filter((v) => v >= b.min && v <= b.max).length,
  );
  const max = Math.max(...counts, 1);
  return (
    <div className="kpi-graphic kpi-hist">
      <div className="kpi-hist-bars">
        {buckets.map((b, i) => (
          <div className="kpi-hist-col" key={b.label}>
            <div className="kpi-hist-track">
              <div
                className="kpi-hist-bar"
                style={{ height: `${(counts[i] / max) * 100}%` }}
              >
                {counts[i] > 0 && <span className="kpi-hist-n">{counts[i]}</span>}
              </div>
            </div>
            <span className="kpi-hist-label">{b.label}</span>
          </div>
        ))}
      </div>
      <p className="kpi-graphic-cap">{caption}</p>
    </div>
  );
}

// One unified, tabbed modal for the whole scorecard. Clicking any KPI tile opens
// it on that metric's tab; the user can click through the others. Each pane pairs
// a graphic (a ring for rates, a distribution histogram for the averages) with the
// people behind the number.
export default function KpiModal({
  stats,
  members,
  initialKey,
  onClose,
}: KpiModalProps) {
  const tabs = useMemo(
    () => stats.filter((s) => s.key && (members[s.key]?.length ?? 0) > 0),
    [stats, members],
  );
  const [activeKey, setActiveKey] = useState(
    tabs.some((t) => t.key === initialKey) ? initialKey : tabs[0]?.key ?? "",
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        if (tabs.length < 2) return;
        e.preventDefault();
        const delta = e.key === "ArrowRight" ? 1 : -1;
        setActiveKey((cur) => {
          const idx = tabs.findIndex((t) => t.key === cur);
          if (idx === -1) return cur;
          return tabs[(idx + delta + tabs.length) % tabs.length].key;
        });
      }
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose, tabs]);

  const stat = tabs.find((t) => t.key === activeKey);
  const people = stat ? members[activeKey] ?? [] : [];
  const hist = HISTOGRAM[activeKey];

  return (
    <div className="sector-modal-backdrop" onClick={onClose} role="presentation">
      <div
        className="sector-modal kpi-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Cohort insight"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="kpi-topbar">
          <div className="kpi-tabs" role="tablist" aria-label="Metrics">
            {tabs.map((t) => (
              <button
                key={t.key}
                type="button"
                role="tab"
                aria-selected={t.key === activeKey}
                className={`kpi-tab${t.key === activeKey ? " is-active" : ""}`}
                onClick={() => setActiveKey(t.key)}
              >
                {TAB_LABEL[t.key] ?? t.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="sector-modal-close kpi-close"
            onClick={onClose}
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        {stat && (
          <div className="sector-modal-body">
            <div className="kpi-pane-top">
              <div className="kpi-hero">
                <div className="kpi-hero-value">{stat.value}</div>
                <div className="kpi-hero-label">{stat.label}</div>
                <p className="kpi-hero-detail">{stat.detail}</p>
                <p className="kpi-hero-count">
                  {people.length} {people.length === 1 ? "alum" : "alumni"}
                </p>
              </div>
              {hist ? (
                <Histogram
                  values={people
                    .map((m) => m.metric)
                    .filter((v): v is number => v != null)}
                  buckets={hist.buckets}
                  caption={hist.caption}
                />
              ) : (
                <Ring pct={stat.pct} />
              )}
            </div>

            <div className="kpi-people">
              {people.map((m) => (
                <Link
                  href={`/person/${m.slug}`}
                  className="sector-member"
                  key={`${m.slug}-${m.name}`}
                >
                  <span className="sector-member-name">{m.name}</span>
                  <span className="sector-member-meta">{m.detail}</span>
                  <span className="sector-member-tag">
                    {m.school} · Titans {m.titanClass}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
