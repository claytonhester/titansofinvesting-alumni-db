"use client";

import { useEffect, useMemo } from "react";
import Link from "next/link";
import type { SectorMember } from "@/lib/db";

interface SectorModalProps {
  title: string;
  subtitle: string;
  members: SectorMember[];
  onClose: () => void;
}

interface SectorGroup {
  sector: string;
  members: SectorMember[];
}

// Group the flat member rows by sector, ordered by group size (largest first) so
// the modal reads like the card — but FULLY expanded: every sector, every person,
// nothing collapsed into a single "Other" line. The catch-all sorts to the end.
function groupBySector(members: SectorMember[]): SectorGroup[] {
  const map = new Map<string, SectorMember[]>();
  for (const m of members) {
    const list = map.get(m.sector) ?? [];
    list.push(m);
    map.set(m.sector, list);
  }
  return [...map.entries()]
    .map(([sector, list]) => ({ sector, members: list }))
    .sort((a, b) => {
      const aCatch = a.sector === "Other / Operating" ? 1 : 0;
      const bCatch = b.sector === "Other / Operating" ? 1 : 0;
      if (aCatch !== bCatch) return aCatch - bCatch;
      return b.members.length - a.members.length;
    });
}

export default function SectorModal({
  title,
  subtitle,
  members,
  onClose,
}: SectorModalProps) {
  const groups = useMemo(() => groupBySector(members), [members]);
  const total = members.length;

  // Escape to close; lock the page scroll while the modal is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [onClose]);

  return (
    <div
      className="sector-modal-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        className="sector-modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sector-modal-head">
          <div>
            <h3 className="sector-modal-title">{title}</h3>
            <p className="sector-modal-sub">
              {subtitle} · {total} {total === 1 ? "alum" : "alumni"} ·{" "}
              {groups.length} {groups.length === 1 ? "cluster" : "clusters"}
            </p>
          </div>
          <button
            type="button"
            className="sector-modal-close"
            onClick={onClose}
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="sector-modal-body">
          {groups.map((g) => (
            <section className="sector-group" key={g.sector}>
              <div className="sector-group-head">
                <span className="sector-group-name">{g.sector}</span>
                <span className="sector-group-count">{g.members.length}</span>
              </div>
              <div className="sector-group-list">
                {g.members.map((m) => (
                  <Link
                    href={`/person/${m.slug}`}
                    className="sector-member"
                    key={`${m.slug}-${m.name}`}
                  >
                    <span className="sector-member-name">{m.name}</span>
                    <span className="sector-member-meta">
                      {m.employer || "—"}
                      {m.industry ? ` · ${m.industry}` : ""}
                    </span>
                    <span className="sector-member-tag">
                      {m.school} · Titans {m.titanClass}
                    </span>
                  </Link>
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>
    </div>
  );
}
