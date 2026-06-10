"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import {
  NEWS_CATEGORIES,
  type NewsCategory,
  type NewsItem,
} from "@/lib/news-types";
import { byEditorialRank } from "@/lib/news-rank";

type Filter = "All" | NewsCategory;

interface NewsFeedProps {
  items: NewsItem[];
}

function byRecency(a: NewsItem, b: NewsItem): number {
  if (a.date && b.date && a.date !== b.date) {
    return a.date < b.date ? 1 : -1;
  }
  return b.relevance - a.relevance;
}

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

function formatDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  const month = MONTHS[Number(m[2]) - 1] ?? "";
  return `${month} ${Number(m[3])}, ${m[1]}`;
}

function PersonChip({ item }: { item: NewsItem }) {
  return (
    <Link href={`/person/${item.nameSlug}`} className="news-person">
      <span className="news-person-name">{item.personName}</span>
      <span className="news-person-meta">
        {item.school}
        {` · Titans ${item.titanClass}`}
      </span>
    </Link>
  );
}

function NewsDate({ item }: { item: NewsItem }) {
  if (!item.date) return null;
  return (
    <time className="news-date" dateTime={item.date}>
      {formatDate(item.date)}
    </time>
  );
}

function SourceLink({ item }: { item: NewsItem }) {
  return (
    <a
      href={item.sourceUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="news-source-link"
    >
      {item.sourceHost || "Read article"}
      <svg
        className="news-source-arrow"
        width="11"
        height="11"
        viewBox="0 0 12 12"
        fill="none"
        aria-hidden="true"
      >
        <path
          d="M3.5 8.5L8.5 3.5M8.5 3.5H4.5M8.5 3.5V7.5"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </a>
  );
}

export default function NewsFeed({ items }: NewsFeedProps) {
  const [filter, setFilter] = useState<Filter>("All");

  const counts = useMemo(() => {
    const map = new Map<NewsCategory, number>();
    for (const item of items) {
      map.set(item.category, (map.get(item.category) ?? 0) + 1);
    }
    return map;
  }, [items]);

  // "All" leads with editorial rank (Recognition first, blended with importance);
  // a specific-category view keeps recency order (newest in that category first).
  const visible = useMemo(
    () =>
      filter === "All"
        ? [...items].sort(byEditorialRank)
        : items.filter((item) => item.category === filter).sort(byRecency),
    [items, filter],
  );

  if (items.length === 0) {
    return (
      <div className="empty">
        No news mentions yet. This feed populates as enrichment runs.
      </div>
    );
  }

  const [lead, ...rest] = visible;

  return (
    <div className="news">
      <div className="news-filters" role="tablist" aria-label="News categories">
        <button
          type="button"
          className={`news-chip${filter === "All" ? " is-active" : ""}`}
          onClick={() => setFilter("All")}
        >
          All
          <span className="news-chip-count">{items.length}</span>
        </button>
        {NEWS_CATEGORIES.map((cat) => {
          const count = counts.get(cat) ?? 0;
          if (count === 0) return null;
          return (
            <button
              key={cat}
              type="button"
              className={`news-chip${filter === cat ? " is-active" : ""}`}
              onClick={() => setFilter(cat)}
            >
              {cat}
              <span className="news-chip-count">{count}</span>
            </button>
          );
        })}
      </div>

      {visible.length === 0 ? (
        <div className="empty">No mentions in this category.</div>
      ) : (
        <>
          {lead && (
            <article className="news-lead">
              <div className="news-lead-body">
                <div className="news-lead-head">
                  <span className={`news-cat news-cat-${slug(lead.category)}`}>
                    {lead.category}
                  </span>
                  <NewsDate item={lead} />
                </div>
                <a
                  href={lead.sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="news-lead-headline"
                >
                  {lead.headline}
                </a>
                {lead.snippet && (
                  <p className="news-lead-snippet">{lead.snippet}</p>
                )}
                <div className="news-lead-foot">
                  <PersonChip item={lead} />
                  <SourceLink item={lead} />
                </div>
              </div>
            </article>
          )}

          {rest.length > 0 && (
            <div className="news-grid">
              {rest.map((item) => (
                <article className="news-card" key={item.id}>
                  <span className={`news-cat news-cat-${slug(item.category)}`}>
                    {item.category}
                  </span>
                  <a
                    href={item.sourceUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="news-card-headline"
                  >
                    {item.headline}
                  </a>
                  {item.snippet && (
                    <p className="news-card-snippet">{item.snippet}</p>
                  )}
                  <div className="news-card-foot">
                    <PersonChip item={item} />
                    <div className="news-card-source">
                      <SourceLink item={item} />
                      <NewsDate item={item} />
                    </div>
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function slug(category: NewsCategory): string {
  return category
    .toLowerCase()
    .replace(/&/g, "and")
    .replace(/[^a-z]+/g, "-")
    .replace(/^-|-$/g, "");
}
