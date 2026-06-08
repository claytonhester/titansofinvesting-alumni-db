import Link from "next/link";
import { notFound } from "next/navigation";
import { getClaimsForPerson, getPersonBySlug } from "@/lib/db";
import { buildResume, linkedinSearchUrl } from "@/lib/resume";

export const dynamic = "force-dynamic";

export default async function PersonPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const person = getPersonBySlug(slug);
  if (!person) notFound();

  const claims = getClaimsForPerson(person.id);
  const resume = buildResume(person, claims);

  const headline =
    resume.currentTitle && resume.currentEmployer
      ? `${resume.currentTitle} · ${resume.currentEmployer}`
      : resume.currentTitle ??
        resume.currentEmployer ??
        person.initial_company;

  const initials = person.full_name
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");

  const linkedinHref = resume.linkedinUrl ?? linkedinSearchUrl(person);
  const linkedinExact = Boolean(resume.linkedinUrl);

  return (
    <main className="wrap resume">
      <Link className="back" href="/">
        ← Back to directory
      </Link>

      {/* ---------- Header ---------- */}
      <header className="resume-header">
        <div className="avatar" aria-hidden>
          {initials}
        </div>
        <div className="resume-id">
          <h1>{person.full_name}</h1>
          <p className="resume-headline">{headline}</p>
          <div className="resume-meta">
            {resume.location && <span>{resume.location}</span>}
            <span>
              {person.school} · Titans {person.titan_class}
            </span>
            {person.needs_review === 1 && (
              <span className="flag">needs review</span>
            )}
          </div>
        </div>
        <div className="resume-actions">
          <a
            className="btn-linkedin"
            href={linkedinHref}
            target="_blank"
            rel="noopener noreferrer"
          >
            <span className="li-mark">in</span>
            {linkedinExact ? "View LinkedIn" : "Find on LinkedIn"}
          </a>
          {!linkedinExact && (
            <span className="action-note">Name search — profile not yet verified</span>
          )}
        </div>
      </header>

      {resume.bio && <p className="resume-bio">{resume.bio}</p>}

      {resume.claimCount === 0 ? (
        <div className="resume-empty">
          <h3>No enriched research yet</h3>
          <p>
            We haven&apos;t assembled a career profile for {person.full_name}{" "}
            from public sources yet. Their directory listing is below.
          </p>
        </div>
      ) : (
        <div className="resume-body">
          <div className="resume-main">
            {resume.experienceGroups.length > 0 && (
              <section className="resume-section">
                <h2 className="resume-section-head">Experience</h2>
                <div className="timeline">
                  {resume.experienceGroups.map((g, gi) =>
                    g.roles.length === 1 ? (
                      <div className="xp" key={`${g.company}-${gi}`}>
                        <div className="xp-rail">
                          <span
                            className={`xp-dot${g.current ? " on" : ""}`}
                          />
                        </div>
                        <div className="xp-body">
                          <div className="xp-dates">
                            {g.roles[0].start ?? (g.current ? "" : "—")}
                            {g.roles[0].end ? ` – ${g.roles[0].end}` : ""}
                            {g.current && (
                              <span className="now-pill">Current</span>
                            )}
                          </div>
                          <p className="xp-title">{g.roles[0].title}</p>
                          {g.company && <p className="xp-co">{g.company}</p>}
                        </div>
                      </div>
                    ) : (
                      <div className="xp xp-group" key={`${g.company}-${gi}`}>
                        <div className="xp-rail">
                          <span
                            className={`xp-dot${g.current ? " on" : ""}`}
                          />
                        </div>
                        <div className="xp-body">
                          <p className="xp-co xp-group-co">{g.company}</p>
                          <div className="xp-dates xp-group-span">
                            {g.start ?? "—"}
                            {g.end ? ` – ${g.end}` : ""}
                          </div>
                          <div className="xp-roles">
                            {g.roles.map((r, ri) => (
                              <div className="xp-role" key={`${r.title}-${ri}`}>
                                <div className="xp-dates">
                                  {r.start ?? (r.current ? "" : "—")}
                                  {r.end ? ` – ${r.end}` : ""}
                                  {r.current && (
                                    <span className="now-pill">Current</span>
                                  )}
                                </div>
                                <p className="xp-title">{r.title}</p>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )
                  )}
                </div>
              </section>
            )}

            {resume.education.length > 0 && (
              <section className="resume-section">
                <h2 className="resume-section-head">Education</h2>
                <div className="edu-list">
                  {resume.education.map((e, i) => (
                    <div className="edu" key={`${e.institution}-${i}`}>
                      <p className="edu-school">{e.institution}</p>
                      {e.degrees.map((d, di) => (
                        <p className="edu-degree" key={`${d}-${di}`}>
                          {d}
                        </p>
                      ))}
                    </div>
                  ))}
                </div>
              </section>
            )}

            {resume.links.length > 0 && (
              <section className="resume-section">
                <h2 className="resume-section-head">Mentions &amp; appearances</h2>
                <div className="links-list">
                  {resume.links.map((l, i) => (
                    <a
                      className="link-row"
                      key={`${l.url}-${i}`}
                      href={l.url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <span className="link-label">{l.label}</span>
                      <span className="link-go">↗</span>
                    </a>
                  ))}
                </div>
              </section>
            )}

            {resume.news.length > 0 && (
              <section className="resume-section">
                <h2 className="resume-section-head">In the news</h2>
                <p className="section-sub">Unverified public mentions</p>
                <div className="links-list">
                  {resume.news.map((n, i) => {
                    let host = n.url;
                    try {
                      host = new URL(n.url).hostname.replace(/^www\./, "");
                    } catch {
                      host = n.url;
                    }
                    return (
                      <a
                        className="link-row news-row"
                        key={`${n.url}-${i}`}
                        href={n.url}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        <span className="news-text">
                          <span className="link-label">{n.headline}</span>
                          <span className="news-meta">
                            {host}
                            {n.date ? ` · ${n.date}` : ""}
                          </span>
                        </span>
                        <span className="link-go">↗</span>
                      </a>
                    );
                  })}
                </div>
              </section>
            )}
          </div>

          {/* ---------- Sidebar ---------- */}
          <aside className="resume-side">
            <div className="side-card">
              <div className="side-stat">
                <div className="side-n">{resume.claimCount}</div>
                <div className="side-l">Source-attributed claims</div>
              </div>
              <div className="side-stat">
                <div className="side-n">
                  {Math.round(resume.avgConfidence * 100)}%
                </div>
                <div className="side-l">Avg. confidence</div>
              </div>
              <div className="side-stat">
                <div className="side-n">{resume.sources.length}</div>
                <div className="side-l">
                  {resume.sources.length === 1 ? "Source" : "Sources"}
                </div>
              </div>
            </div>

            {resume.sources.length > 0 && (
              <div className="side-sources">
                <div className="side-sources-head">Sourced from</div>
                {resume.sources.map((s, i) => {
                  let host = s;
                  try {
                    host = new URL(s).hostname.replace(/^www\./, "");
                  } catch {
                    host = s;
                  }
                  return (
                    <a
                      key={`${s}-${i}`}
                      href={s}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="side-source"
                    >
                      {host}
                    </a>
                  );
                })}
              </div>
            )}
          </aside>
        </div>
      )}

      <div className="provenance">
        Assembled from the public Titans of Investing class directory and
        source-attributed public research.{" "}
        <a href={person.source_url} target="_blank" rel="noopener noreferrer">
          View source directory
        </a>
        .
        {person.needs_review === 1 && (
          <>
            {" "}
            This entry did not split cleanly into name / company / city and is
            flagged for manual review.
          </>
        )}
      </div>
    </main>
  );
}
