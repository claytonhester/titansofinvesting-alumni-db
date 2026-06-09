import Link from "next/link";
import { notFound } from "next/navigation";
import { getCompanyBySlug, titansAtCompany, type TitanLink } from "@/lib/db";
import { smartTitle } from "@/lib/normalize";

export const dynamic = "force-dynamic";

function titleCaseLocation(loc: string): string {
  // "boston, massachusetts, united states" -> "Boston, Massachusetts, United States"
  return loc
    .split(",")
    .map((part) => smartTitle(part.trim()))
    .join(", ");
}

function sizeLabel(size: string, count: number | null): string {
  if (size && count) return `${size} employees (${count.toLocaleString()})`;
  if (size) return `${size} employees`;
  if (count) return `${count.toLocaleString()} employees`;
  return "";
}

function tenure(t: TitanLink): string {
  if (t.is_current) return t.start_year ? `since ${t.start_year}` : "current";
  if (t.start_year || t.end_year) return `${t.start_year ?? "?"}–${t.end_year ?? "?"}`;
  return "";
}

function titanRow(t: TitanLink) {
  const meta = [t.title && smartTitle(t.title), tenure(t), `${t.school} · Titans ${t.titan_class}`]
    .filter(Boolean)
    .join(" · ");
  return (
    <Link
      key={`${t.name_slug}-${t.start_year ?? ""}-${t.title}`}
      className="link-row news-row"
      href={`/person/${t.name_slug}`}
    >
      <span className="news-text">
        <span className="link-label">{t.full_name}</span>
        <span className="news-meta">{meta}</span>
      </span>
      <span className="link-go">→</span>
    </Link>
  );
}

export default async function CompanyPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const company = getCompanyBySlug(slug);
  if (!company) notFound();

  const { current, past } = titansAtCompany(company.domain);
  const totalTitans = current.length + past.length;
  const displayName = smartTitle(company.name);  // "...Ltd., Llp" -> "...Ltd., LLP"

  // PDL summaries are all-lowercase and can run long; capitalize the first letter
  // and trim to a clean paragraph (full text stays in the source record).
  const rawSummary = company.summary.trim();
  const summary = rawSummary
    ? (() => {
        const capped =
          rawSummary.length > 420
            ? rawSummary.slice(0, 420).replace(/\s+\S*$/, "") + "…"
            : rawSummary;
        return capped.charAt(0).toUpperCase() + capped.slice(1);
      })()
    : "";
  const initials = company.name
    .replace(/[^a-zA-Z0-9 ]/g, "")
    .split(/\s+/)
    .slice(0, 2)
    .map((w) => w[0]?.toUpperCase() ?? "")
    .join("");

  // PDL types come as snake_case ("public_subsidiary") — clean to words + Title
  // Case, and append the ticker for any public flavor.
  const typeClean = company.companyType
    ? smartTitle(company.companyType.replace(/_/g, " "))
    : null;
  const typePill = typeClean
    ? company.companyType.startsWith("public") && company.ticker
      ? `${typeClean} · ${company.ticker}`
      : typeClean
    : null;

  const cards: { label: string; value: string }[] = [];
  const size = sizeLabel(company.size, company.employeeCount);
  if (size) cards.push({ label: "Size", value: size });
  if (company.industry) cards.push({ label: "Industry", value: smartTitle(company.industry) });
  if (company.founded) cards.push({ label: "Founded", value: String(company.founded) });
  if (typePill) cards.push({ label: "Type", value: typePill });
  if (company.hqLocation)
    cards.push({ label: "Headquarters", value: titleCaseLocation(company.hqLocation) });

  return (
    <main className="wrap resume">
      <Link className="back" href="/">
        ← Back to directory
      </Link>

      <header className="resume-header">
        <div className="avatar" aria-hidden>
          {initials}
        </div>
        <div className="resume-id">
          <h1>{displayName}</h1>
          <p className="resume-headline">
            {company.industry ? smartTitle(company.industry) : "Company"}
          </p>
          <div className="resume-meta">
            {company.hqLocation && <span>{titleCaseLocation(company.hqLocation)}</span>}
            {typePill && <span>{typePill}</span>}
          </div>
        </div>
        {company.linkedinUrl && (
          <div className="resume-actions">
            <a
              className="btn-linkedin"
              href={`https://${company.linkedinUrl.replace(/^https?:\/\//, "")}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <span className="li-mark">in</span> Company LinkedIn
            </a>
          </div>
        )}
      </header>

      <div className="resume-body">
        <div className="resume-main">
          {summary && (
            <section className="resume-section">
              <h2 className="resume-section-head">About</h2>
              <p className="resume-bio">{summary}</p>
            </section>
          )}

          <section className="resume-section">
            <h2 className="resume-section-head">
              Titans here now{current.length ? ` · ${current.length}` : ""}
            </h2>
            {current.length === 0 ? (
              <p className="section-sub">No Titans currently on record at this firm.</p>
            ) : (
              <div className="links-list">{current.map(titanRow)}</div>
            )}
          </section>

          {past.length > 0 && (
            <section className="resume-section">
              <h2 className="resume-section-head">
                Titans who were here · {past.length}
              </h2>
              <div className="links-list">{past.map(titanRow)}</div>
            </section>
          )}
        </div>

        <aside className="resume-side">
          {cards.map((card) => (
            <div className="side-card" key={card.label}>
              <div className="side-stat">
                <div className="side-n">{card.value}</div>
                <div className="side-l">{card.label}</div>
              </div>
            </div>
          ))}
          <div className="side-card">
            <p className="side-attribution">
              Firmographics from People Data Labs, enriched once per firm and shared
              across {totalTitans} Titan{totalTitans === 1 ? "" : "s"}.
            </p>
          </div>
        </aside>
      </div>
    </main>
  );
}
