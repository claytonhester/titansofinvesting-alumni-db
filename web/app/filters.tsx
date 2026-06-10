"use client";

import { useRouter } from "next/navigation";
import { useCallback } from "react";
import type { ClassOption } from "@/lib/db";

interface Props {
  schools: string[];
  classes: ClassOption[];
  current: { q: string; school: string; titanClass: string; enrichedOnly: boolean };
}

export default function Filters({ schools, classes, current }: Props) {
  const router = useRouter();

  const submit = useCallback(
    (form: HTMLFormElement) => {
      const data = new FormData(form);
      const params = new URLSearchParams();
      const q = (data.get("q") as string)?.trim();
      const school = data.get("school") as string;
      const titanClass = data.get("titanClass") as string;
      // Checkbox is present in FormData only when checked. Enriched-only is the
      // default, so we only carry a param when the user turns it OFF.
      const enrichedOnly = data.get("enrichedOnly") !== null;
      if (q) params.set("q", q);
      if (school) params.set("school", school);
      if (titanClass) params.set("class", titanClass);
      if (!enrichedOnly) params.set("enriched", "0");
      const qs = params.toString();
      router.push(qs ? `/?${qs}` : "/");
    },
    [router]
  );

  const visibleClasses = current.school
    ? classes.filter((c) => c.school === current.school)
    : classes;

  return (
    <form
      className="toolbar"
      onSubmit={(e) => {
        e.preventDefault();
        submit(e.currentTarget);
      }}
    >
      <input
        type="search"
        name="q"
        placeholder="Search name, company, or city…"
        defaultValue={current.q}
        aria-label="Search"
      />
      <select name="school" defaultValue={current.school} aria-label="School">
        <option value="">All schools</option>
        {schools.map((s) => (
          <option key={s} value={s}>
            {s}
          </option>
        ))}
      </select>
      <select name="titanClass" defaultValue={current.titanClass} aria-label="Class">
        <option value="">All classes</option>
        {visibleClasses.map((c) => (
          <option key={`${c.school}-${c.titan_class}`} value={String(c.titan_class)}>
            {c.school} · Titans {c.titan_class} ({c.count})
          </option>
        ))}
      </select>
      <button type="submit">Apply</button>
      <button
        type="button"
        className="reset"
        onClick={() => router.push("/")}
      >
        Reset
      </button>
      <label className="filter-toggle" title="Hide alumni we don't have data on yet">
        <input
          type="checkbox"
          name="enrichedOnly"
          defaultChecked={current.enrichedOnly}
          onChange={(e) => submit(e.currentTarget.form as HTMLFormElement)}
        />
        <span>Only profiles with data</span>
      </label>
    </form>
  );
}
