import Link from "next/link";
import {
  directoryStats,
  listClasses,
  listPeople,
  listSchools,
  recentlyEnriched,
  newsCount,
} from "@/lib/db";
import { getNewsFeed } from "@/lib/news";
import { getAlumniInsights } from "@/lib/insights";
import { mintChatToken } from "@/lib/chat/auth";
import Filters from "./filters";
import Tabs from "./tabs";
import NewsFeed from "./news-feed";
import InsightsViews from "./insights-views";
import ChatBar from "./chat-bar";

export const dynamic = "force-dynamic";

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const first = parts[0][0] ?? "";
  const last = parts.length > 1 ? parts[parts.length - 1][0] ?? "" : "";
  return (first + last).toUpperCase();
}

interface SearchParams {
  q?: string;
  school?: string;
  class?: string;
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const sp = await searchParams;
  const q = sp.q?.trim() ?? "";
  const school = sp.school ?? "";
  const classRaw = sp.class ?? "";
  const titanClass = classRaw === "" ? undefined : Number(classRaw);

  const stats = directoryStats();
  const schools = listSchools();
  const classes = listClasses();
  const enriched = recentlyEnriched(6);
  const insights = getAlumniInsights();
  const newsFeed = getNewsFeed(40);
  const newsTotal = newsCount();
  const people = listPeople({
    q: q || undefined,
    school: school || undefined,
    titanClass: Number.isNaN(titanClass) ? undefined : titanClass,
  });

  const filtered = Boolean(q || school || classRaw);
  const enrichPct = stats.total
    ? Math.round((stats.enriched / stats.total) * 1000) / 10
    : 0;

  return (
    <>
      <header className="hero">
        <div className="wrap">
          <p className="eyebrow">Titans of Investing · Alumni Intelligence</p>
          <h1>The people behind the program, in one view.</h1>
          <p className="tagline">
            A searchable, analytical directory of Titans of Investing alumni —
            assembled from the public class directory and enriched with
            source-attributed career data.
          </p>

          <ChatBar token={mintChatToken()} />
        </div>
      </header>

      <main className="wrap">
        <Tabs
          defaultTab={filtered ? "directory" : "overview"}
          tabs={[
            { key: "overview", label: "Overview & Insights" },
            { key: "directory", label: "Titan Directory", badge: people.length },
            {
              key: "news",
              label: "In the news",
              badge: newsFeed.isSample ? newsFeed.items.length : newsTotal,
            },
            { key: "build", label: "Build Status" },
          ]}
          panels={{
            overview: (
              <InsightsViews
                narrative={insights.narrative}
                hasOutcomeData={insights.hasOutcomeData}
                startFirms={insights.startFirms}
                landingFirms={insights.landingFirms}
                landingSectors={insights.landingSectors}
                seniority={insights.seniority}
                currentTitles={insights.currentTitles}
                signatureStats={insights.signatureStats}
                clusters={insights.clusters}
                geoSpread={insights.geoSpread}
                schoolSpread={insights.schoolSpread}
                measuredSectors={insights.measuredSectors}
              />
            ),
            build: (
        <section className="section">
          <div className="dash">
            <div className="panel col-12">
              <h3>Directory at a glance</h3>
              <div className="stat-row">
                <div className="stat-cell">
                  <div className="n">{stats.total.toLocaleString()}</div>
                  <div className="l">Alumni</div>
                </div>
                <div className="stat-cell">
                  <div className="n">{stats.classes}</div>
                  <div className="l">Classes</div>
                </div>
                <div className="stat-cell">
                  <div className="n">{stats.schools}</div>
                  <div className="l">Schools</div>
                </div>
                <div className="stat-cell">
                  <div className="n">{stats.claims.toLocaleString()}</div>
                  <div className="l">Verified claims</div>
                </div>
              </div>
            </div>
            <div className="panel col-12">
              <h3>Enrichment coverage</h3>
              <div className="enrich-meter">
                <div className="big">{enrichPct}%</div>
                <div className="sub">
                  {stats.enriched.toLocaleString()} of{" "}
                  {stats.total.toLocaleString()} alumni enriched ·{" "}
                  {stats.sources.toLocaleString()} sources
                </div>
                <div className="enrich-track">
                  <div
                    className="enrich-fill"
                    style={{ width: `${Math.max(enrichPct, 1.5)}%` }}
                  />
                </div>
              </div>
              <div className="enriched-people">
                {enriched.map((p) => (
                  <Link key={p.name_slug} href={`/person/${p.name_slug}`}>
                    <span className="who">{p.full_name}</span>
                    <span className="badge">{p.claim_count} claims</span>
                  </Link>
                ))}
              </div>
            </div>
          </div>
        </section>
            ),
            directory: (
        <section className="section">
          <Filters
            schools={schools}
            classes={classes}
            current={{ q, school, titanClass: classRaw }}
          />

          {people.length === 0 ? (
            <div className="empty">No alumni match those filters.</div>
          ) : (
            <div className="grid">
              {people.map((p) => (
                <Link
                  key={p.id}
                  href={`/person/${p.name_slug}`}
                  className="card"
                >
                  <div className="card-id">
                    <span className="avatar" aria-hidden="true">
                      {initials(p.full_name)}
                    </span>
                    <div className="card-id-text">
                      <p className="name">{p.full_name}</p>
                      <p className="company">{p.initial_company}</p>
                    </div>
                  </div>
                  <div className="meta">
                    <span className="tag">
                      {p.school} · Titans {p.titan_class}
                    </span>
                    {p.city !== "(unknown)" && (
                      <span className="loc">
                        <span className="loc-dot" aria-hidden="true" />
                        {p.city}
                      </span>
                    )}
                    {p.needs_review === 1 && (
                      <span className="tag review">needs review</span>
                    )}
                  </div>
                </Link>
              ))}
            </div>
          )}
        </section>
            ),
            news: (
        <section className="section">
          <NewsFeed items={newsFeed.items} isSample={newsFeed.isSample} />
        </section>
            ),
          }}
        />
      </main>
    </>
  );
}
