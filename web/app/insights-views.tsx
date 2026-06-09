"use client";

import { useState } from "react";
import type {
  FirmCount,
  SeniorityTier,
  CurrentTitle,
  SignatureStat,
} from "@/lib/insights";
import type { FirmCluster } from "@/lib/db";

interface FirmBar {
  company: string;
  count: number;
}
interface GeoBar {
  city: string;
  count: number;
}
interface SchoolBar {
  school: string;
  count: number;
}
interface SectorBar {
  sector: string;
  count: number;
}

interface InsightsViewsProps {
  narrative: string;
  hasOutcomeData: boolean;
  startFirms: FirmBar[];
  landingFirms: FirmCount[];
  landingSectors: SectorBar[];
  seniority: SeniorityTier[];
  currentTitles: CurrentTitle[];
  signatureStats: SignatureStat[];
  clusters: FirmCluster[];
  geoSpread: GeoBar[];
  schoolSpread: SchoolBar[];
  measuredSectors: SectorBar[];
}

function max(values: number[]): number {
  return Math.max(...values, 1);
}

function EmptyState({ title, note }: { title: string; note: string }) {
  return (
    <div className="insight-empty">
      <p className="insight-empty-title">{title}</p>
      <p className="insight-empty-note">{note}</p>
    </div>
  );
}

function FirmList({ firms }: { firms: { company: string; count: number }[] }) {
  return (
    <div className="firm-list">
      {firms.map((f, i) => (
        <div className="firm-row" key={f.company}>
          <span className="rank">{i + 1}</span>
          <span className="name">{f.company}</span>
          <span className="count">{f.count}</span>
        </div>
      ))}
    </div>
  );
}

function Bars({
  rows,
}: {
  rows: { label: string; count: number }[];
}) {
  const m = max(rows.map((r) => r.count));
  return (
    <div className="bars">
      {rows.map((r) => (
        <div className="bar-row" key={r.label}>
          <div className="bar-top">
            <span>{r.label}</span>
            <span className="v">{r.count.toLocaleString()}</span>
          </div>
          <div className="bar-track">
            <div
              className="bar-fill"
              style={{ width: `${(r.count / m) * 100}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function InsightsViews(props: InsightsViewsProps) {
  const geoRows = props.geoSpread.map((g) => ({ label: g.city, count: g.count }));
  const schoolRows = props.schoolSpread.map((s) => ({
    label: s.school,
    count: s.count,
  }));
  const sectorRows = props.measuredSectors.map((s) => ({
    label: s.sector,
    count: s.count,
  }));
  const landingSectorRows = props.landingSectors.map((s) => ({
    label: s.sector,
    count: s.count,
  }));
  const titleFirms = props.currentTitles.map((t) => ({
    company: t.title,
    count: t.count,
  }));
  const ladderMax = max(props.seniority.map((s) => s.count));

  const [view, setView] = useState<"origins" | "outcomes" | "map">("origins");

  const hasNarrative = props.hasOutcomeData && props.narrative.trim().length > 0;
  const hasScorecard = props.signatureStats.length > 0;

  return (
    <section className="section">
      <div className="dash">
        <div className="panel col-12 insight-synthesis">
          <div className="insight-synthesis-head">
            <h3>Titans Over Time</h3>
          </div>
          {hasNarrative ? (
            <p className="insight-narrative">{props.narrative}</p>
          ) : (
            <EmptyState
              title="No cohort summary yet"
              note="This narrative is generated from real enrichment data. Enrich alumni to see how the cohort's story takes shape."
            />
          )}
        </div>

        {hasScorecard ? (
          <div className="scorecard-bento col-12">
            {props.signatureStats.map((s) => (
              <div className="score-tile" key={s.label}>
                <div className="score-value">{s.value}</div>
                <div className="score-label">{s.label}</div>
                <div className="score-detail">{s.detail}</div>
                {s.pct > 0 && (
                  <div className="score-bar-track">
                    <div
                      className="score-bar-fill"
                      style={{ width: `${Math.min(s.pct, 100)}%` }}
                    />
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="panel col-12">
            <EmptyState
              title="No scorecard yet"
              note="Buy-side, MD+, founders and first-firm KPIs are measured per person during enrichment. They appear here once alumni are classified."
            />
          </div>
        )}

        <div className="insight-tabs col-12">
          <button
            type="button"
            className={`insight-tab${view === "origins" ? " is-active" : ""}`}
            onClick={() => setView("origins")}
          >
            Origins
            <span className="insight-tab-note">measured</span>
          </button>
          <button
            type="button"
            className={`insight-tab${view === "outcomes" ? " is-active" : ""}`}
            onClick={() => setView("outcomes")}
          >
            Outcomes
            <span className="insight-tab-note">measured</span>
          </button>
          <button
            type="button"
            className={`insight-tab${view === "map" ? " is-active" : ""}`}
            onClick={() => setView("map")}
          >
            Map
            <span className="insight-tab-note">measured</span>
          </button>
        </div>

        {view === "origins" && (
          <>
            <div className="panel col-6">
              <div className="insight-synthesis-head">
                <h3>Where they start</h3>
                <span className="col-tag">first employer · verified</span>
              </div>
              {props.startFirms.length > 0 ? (
                <FirmList firms={props.startFirms} />
              ) : (
                <EmptyState
                  title="No first employers yet"
                  note="We don't assume the directory's listed company is a real first job. First post-grad employers are confirmed during enrichment."
                />
              )}
            </div>

            <div className="panel col-6">
              <div className="insight-synthesis-head">
                <h3>Where their first jobs cluster</h3>
                <span className="col-tag">first-employer sector · verified</span>
              </div>
              {sectorRows.length > 0 ? (
                <Bars rows={sectorRows} />
              ) : (
                <EmptyState
                  title="No first-job sectors yet"
                  note="Built from verified first employers — appears as alumni are enriched."
                />
              )}
            </div>

            <div className="panel col-12">
              <div className="insight-synthesis-head">
                <h3>By school</h3>
                <span className="col-tag">measured</span>
              </div>
              <Bars rows={schoolRows} />
            </div>
          </>
        )}

        {view === "outcomes" && (
          <>
            <div className="panel col-6">
              <div className="insight-synthesis-head">
                <h3>Where they land</h3>
                <span className="col-tag">current employer · measured</span>
              </div>
              {props.landingFirms.length > 0 ? (
                <FirmList firms={props.landingFirms} />
              ) : (
                <EmptyState
                  title="No landing firms yet"
                  note="Current employers are collected during enrichment."
                />
              )}
            </div>

            <div className="panel col-6">
              <div className="insight-synthesis-head">
                <h3>What they&rsquo;re doing now</h3>
                <span className="col-tag">current title · measured</span>
              </div>
              {titleFirms.length > 0 ? (
                <FirmList firms={titleFirms} />
              ) : (
                <EmptyState
                  title="No current titles yet"
                  note="Current titles are collected during enrichment."
                />
              )}
            </div>

            <div className="panel col-6">
              <div className="insight-synthesis-head">
                <h3>Where they land, by sector</h3>
                <span className="col-tag">current employer · measured</span>
              </div>
              {landingSectorRows.length > 0 ? (
                <Bars rows={landingSectorRows} />
              ) : (
                <EmptyState
                  title="No landing sectors yet"
                  note="Built from verified current employers — appears as alumni are enriched."
                />
              )}
            </div>

            <div className="panel col-6">
              <div className="insight-synthesis-head">
                <h3>Where Titans cluster</h3>
                <span className="col-tag">who&rsquo;s where · measured</span>
              </div>
              {props.clusters.length > 0 ? (
                <div className="cluster-list">
                  {props.clusters.map((c) => (
                    <div className="cluster-row" key={c.company}>
                      <div className="cluster-head">
                        <span className="cluster-firm">{c.company}</span>
                        <span className="cluster-count">{c.count}</span>
                      </div>
                      <div className="cluster-members">
                        {c.members.join(", ")}
                        {c.count > c.members.length
                          ? ` +${c.count - c.members.length} more`
                          : ""}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="No clusters yet"
                  note="Once two or more Titans share a current employer, they show up here — your shortcut to a warm intro."
                />
              )}
            </div>

            <div className="panel col-12">
              <div className="insight-synthesis-head">
                <h3>How far they climb</h3>
                <span className="col-tag">seniority ladder · measured</span>
              </div>
              {props.seniority.length > 0 ? (
                <div className="ladder">
                  {props.seniority.map((s) => (
                    <div className="ladder-row" key={s.tier}>
                      <span className="ladder-tier">{s.tier}</span>
                      <div className="ladder-track">
                        <div
                          className="ladder-fill"
                          style={{ width: `${(s.count / ladderMax) * 100}%` }}
                        />
                      </div>
                      <span className="ladder-v">{s.count.toLocaleString()}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="No seniority ladder yet"
                  note="The ladder is built from enriched current titles."
                />
              )}
            </div>
          </>
        )}

        {view === "map" && (
          <div className="panel col-12">
            <div className="insight-synthesis-head">
              <h3>Where they are</h3>
              <span className="col-tag">current location · measured</span>
            </div>
            {geoRows.length > 0 ? (
              <Bars rows={geoRows} />
            ) : (
              <EmptyState
                title="No current locations yet"
                note="Where alumni live now is collected during enrichment — not assumed from their program-era city. This map fills in as people are enriched."
              />
            )}
          </div>
        )}
      </div>
    </section>
  );
}
