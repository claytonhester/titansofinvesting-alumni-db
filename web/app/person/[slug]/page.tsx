import Link from "next/link";
import { notFound } from "next/navigation";
import {
  curatedNewsForPerson,
  getClaimsForPerson,
  getCompanyForPerson,
  getPersonBySlug,
} from "@/lib/db";
import { smartTitle } from "@/lib/normalize";
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
  // The "In the news" section reads the CURATED feed (the same Haiku editorial
  // gate the homepage tab uses), not the raw news_mention claims — so a bio page
  // or passing mention the curator dropped never resurfaces on the profile.
  const personNews = curatedNewsForPerson(person.id);
  // The current employer's enriched firm record (cached company layer) — drives a
  // clickable firm chip linking to the company page. null when unmatched.
  const company = getCompanyForPerson(person.id);
  const companyChip = company
    ? [
        company.industry && smartTitle(company.industry),
        company.size && `${company.size}`,
        company.hqLocation && smartTitle(company.hqLocation.split(",")[0]),
      ]
        .filter(Boolean)
        .join(" · ")
    : "";

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
          {company && (
            <Link className="company-chip" href={`/company/${company.slug}`}>
              <span className="company-chip-name">{company.name}</span>
              {companyChip && (
                <span className="company-chip-meta">{companyChip}</span>
              )}
              <span className="link-go">→</span>
            </Link>
          )}
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
                          {g.company && (
                            g.current && company ? (
                              <Link
                                className="xp-title is-current"
                                href={`/company/${company.slug}`}
                              >
                                {g.company}
                              </Link>
                            ) : (
                              <p className={`xp-title${g.current ? " is-current" : ""}`}>
                                {g.company}
                              </p>
                            )
                          )}
                          <div className="xp-role-row">
                            <p className={`xp-co${g.current ? " is-current" : ""}`}>
                              {g.roles[0].title}
                            </p>
                            {g.current ? (
                              <span className="now-pill">
                                {g.roles[0].start
                                  ? `${g.roles[0].start} – Present`
                                  : "Present"}
                              </span>
                            ) : (
                              <span className="xp-dates">
                                {g.roles[0].start ?? ""}
                                {g.roles[0].end ? ` – ${g.roles[0].end}` : ""}
                              </span>
                            )}
                          </div>
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
                          {g.company && (
                            g.current && company ? (
                              <Link
                                className="xp-title is-current"
                                href={`/company/${company.slug}`}
                              >
                                {g.company}
                              </Link>
                            ) : (
                              <p className={`xp-title${g.current ? " is-current" : ""}`}>
                                {g.company}
                              </p>
                            )
                          )}
                          <div className="xp-role-row">
                            <p className={`xp-co${g.current ? " is-current" : ""}`}>
                              {g.roles[0].title}
                            </p>
                            {g.current ? (
                              <span className="now-pill">
                                {g.roles[0].start
                                  ? `${g.roles[0].start} – Present`
                                  : "Present"}
                              </span>
                            ) : (
                              <span className="xp-dates">
                                {g.roles[0].start ?? ""}
                                {g.roles[0].end ? ` – ${g.roles[0].end}` : ""}
                              </span>
                            )}
                          </div>
                          {g.roles.length > 1 && (
                            <div className="xp-roles">
                              {g.roles.slice(1).map((r, ri) => (
                                <div className="xp-role" key={`${r.title}-${ri}`}>
                                  <span className="xp-role-title">{r.title}</span>
                                  <span className="xp-role-date">
                                    {r.start ?? ""}
                                    {r.end ? ` – ${r.end}` : ""}
                                  </span>
                                </div>
                              ))}
                            </div>
                          )}
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

            {personNews.length > 0 && (
              <section className="resume-section">
                <h2 className="resume-section-head">In the news</h2>
                <p className="section-sub">
                  Curated press where {person.full_name.split(/\s+/)[0]} is the
                  subject
                </p>
                <div className="links-list">
                  {personNews.map((n, i) => (
                    <a
                      className="link-row news-row"
                      key={`${n.source_url}-${i}`}
                      href={n.source_url}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      <span className="news-text">
                        <span className="link-label">{n.headline}</span>
                        {n.summary && (
                          <span className="news-summary">{n.summary}</span>
                        )}
                        <span className="news-meta">
                          {n.category} · {n.source_host}
                          {n.date ? ` · ${n.date}` : ""}
                        </span>
                      </span>
                      <span className="link-go">↗</span>
                    </a>
                  ))}
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
