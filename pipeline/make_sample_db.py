#!/usr/bin/env python3
"""
Generate a synthetic sample database for open-source release.

This script creates a fully-synthetic sample DB (~15 fake people) with the same
schema as the production database, suitable for open-source distribution. The
sample DB has believable-but-fake data and renders all pages correctly.

The process:
1. Copy the real DB to a temp working file
2. Select ~15 people chosen for diversity (mix of sectors, seniority levels)
3. Delete all other people and their dependent rows
4. Anonymize every PII / free-text column
5. Regenerate insights_snapshot to match the 15 anonymized people
6. Convert journal_mode to DELETE (read-only safe) and remove -wal/-shm sidecars
7. Write result to web/data/sample.db

Usage:
  python make_sample_db.py [source_db_path] [output_db_path]

Examples:
  python make_sample_db.py web/data/titans.db web/data/sample.db
  python make_sample_db.py pipeline/data/titans.db web/data/sample.db
"""

import sqlite3
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================================
# Configuration: Synthetic Data
# ============================================================================

FAKE_NAMES = [
    ("Alex Mitchell", "alex-mitchell"),
    ("Jordan Blake", "jordan-blake"),
    ("Casey Morgan", "casey-morgan"),
    ("Riley Davis", "riley-davis"),
    ("Taylor Adams", "taylor-adams"),
    ("Morgan Stewart", "morgan-stewart"),
    ("Parker Wilson", "parker-wilson"),
    ("Quinn Foster", "quinn-foster"),
    ("Cameron Reed", "cameron-reed"),
    ("Dakota Long", "dakota-long"),
    ("Skyler Kane", "skyler-kane"),
    ("Harper Stone", "harper-stone"),
    ("Avery Knight", "avery-knight"),
    ("Finley Price", "finley-price"),
    ("Sage Wright", "sage-wright"),
]

FAKE_SCHOOLS = [
    "State University",
    "Lone Star College",
    "Gulf Coast University",
    "Central Texas College",
]

FAKE_COMPANIES = [
    "Acme Capital Partners",
    "Nexus Global Advisors",
    "Titan Asset Management",
    "Frontier Investment Group",
    "Sterling Equity Partners",
    "Compass Financial Solutions",
    "Pinnacle Advisory Corp",
    "Zenith Capital",
    "Valor Group",
    "Quantum Partners",
]

FAKE_CITIES = [
    "Austin, Texas",
    "Houston, Texas",
    "Dallas, Texas",
    "San Antonio, Texas",
    "San Francisco, California",
    "New York, New York",
    "Chicago, Illinois",
]

FAKE_DOMAINS = [
    "acme-capital.example",
    "nexus-advisors.example",
    "titan-asset.example",
    "frontier-group.example",
    "sterling-equity.example",
    "compass-financial.example",
    "pinnacle-advisory.example",
    "zenith-capital.example",
    "valor-group.example",
    "quantum-partners.example",
]

FAKE_TITLES = [
    "Analyst",
    "Senior Analyst",
    "Vice President",
    "Senior Vice President",
    "Director",
    "Managing Director",
    "Chief Investment Officer",
    "Portfolio Manager",
    "Investment Manager",
    "Associate",
]

FAKE_EMPLOYERS = [
    "Acme Capital",
    "Nexus Advisors",
    "Titan Asset",
    "Frontier Group",
    "Sterling Equity",
    "Compass Financial",
]

# ============================================================================
# Helper Functions
# ============================================================================


def sanitize_url(url: str) -> str:
    """Convert any real URL to a generic example.com placeholder."""
    return "https://example.com/sample"


def anonymize_text(text: str, prefix: str = "sample") -> str:
    """Replace identifying text with generic synthetic content."""
    if not text or len(text.strip()) < 2:
        return ""
    # Return generic text matching claim_type or column context
    return f"{prefix}_content"


def pick_fake(lst: List[str], idx: int) -> str:
    """Pick a fake value from a list using person_id as deterministic index."""
    return lst[idx % len(lst)]


# ============================================================================
# Main: Sample Generation
# ============================================================================


def generate_sample_db(
    source_db_path: str, output_db_path: str, sample_count: int = 15
) -> None:
    """
    Generate a synthetic sample database.

    Args:
        source_db_path: Path to the real titans.db
        output_db_path: Path to write sample.db
        sample_count: Number of people to keep (default 15)
    """
    # Ensure source exists
    if not os.path.exists(source_db_path):
        raise FileNotFoundError(f"Source DB not found: {source_db_path}")

    # Create output directory
    os.makedirs(os.path.dirname(output_db_path) or ".", exist_ok=True)

    # Work in a temp file first
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tmp:
        temp_db_path = tmp.name

    try:
        # Copy source to temp
        shutil.copy2(source_db_path, temp_db_path)
        print(f"Copied {source_db_path} -> {temp_db_path}")

        conn = sqlite3.connect(temp_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # ====================================================================
        # Step 1: Select diverse people to keep (by sector + seniority)
        # ====================================================================
        cursor.execute(
            """
            SELECT DISTINCT p.id, pi.current_sector, pi.peak_level
            FROM people p
            LEFT JOIN person_insights pi ON p.id = pi.person_id
            WHERE EXISTS (SELECT 1 FROM claims c WHERE c.person_id = p.id)
            ORDER BY pi.current_sector, pi.peak_level DESC
            LIMIT ?
            """,
            (sample_count,),
        )
        sample_people = [dict(row) for row in cursor.fetchall()]
        sample_ids = [row["id"] for row in sample_people]

        if not sample_ids:
            raise ValueError(
                f"Could not find {sample_count} enriched people in source DB"
            )

        print(f"Selected {len(sample_ids)} people for sample: {sample_ids}")

        # ====================================================================
        # Step 2: Anonymize the sample people
        # ====================================================================
        print("Anonymizing people...")
        cursor.execute(
            """
            SELECT id, full_name, name_slug, school, initial_company, city
            FROM people
            WHERE id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        people_to_anonymize = [dict(row) for row in cursor.fetchall()]

        for i, person_row in enumerate(people_to_anonymize):
            pid = person_row["id"]
            fake_name, fake_slug = FAKE_NAMES[i]
            fake_school = pick_fake(FAKE_SCHOOLS, pid)
            fake_company = pick_fake(FAKE_COMPANIES, pid)
            fake_city = pick_fake(FAKE_CITIES, pid)

            cursor.execute(
                """
                UPDATE people
                SET full_name = ?, name_slug = ?, school = ?,
                    initial_company = ?, city = ?, source_url = ?,
                    raw_entry = '', research_company = ?
                WHERE id = ?
                """,
                (
                    fake_name,
                    fake_slug,
                    fake_school,
                    fake_company,
                    fake_city,
                    sanitize_url(""),
                    pick_fake(FAKE_COMPANIES, pid),
                    pid,
                ),
            )

        # ====================================================================
        # Step 3: Anonymize claims
        # ====================================================================
        print("Anonymizing claims...")
        cursor.execute(
            """
            SELECT id, claim_type, person_id
            FROM claims
            WHERE person_id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        claims_to_anonymize = [dict(row) for row in cursor.fetchall()]

        for claim_row in claims_to_anonymize:
            claim_id = claim_row["id"]
            claim_type = claim_row["claim_type"]

            # Generate synthetic value based on claim type
            if claim_type == "current_employer":
                value = pick_fake(FAKE_COMPANIES, claim_row["person_id"])
            elif claim_type == "current_title":
                value = pick_fake(FAKE_TITLES, claim_row["person_id"])
            elif claim_type == "education":
                value = f"Bachelor's Degree from {pick_fake(FAKE_SCHOOLS, claim_row['person_id'])}"
            elif claim_type in ("skill", "public_links", "news_mention"):
                value = f"sample_{claim_type}"
            else:
                value = f"sample_{claim_type}"

            cursor.execute(
                """
                UPDATE claims
                SET value = ?, quote = ?, source_url = ?
                WHERE id = ?
                """,
                (value, "", sanitize_url(""), claim_id),
            )

        # ====================================================================
        # Step 4: Anonymize person_company
        # ====================================================================
        print("Anonymizing person_company...")
        cursor.execute(
            """
            SELECT person_id, domain
            FROM person_company
            WHERE person_id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        pc_rows = [dict(row) for row in cursor.fetchall()]

        for pc_row in pc_rows:
            pid = pc_row["person_id"]
            fake_company_name = pick_fake(FAKE_COMPANIES, pid)
            fake_title = pick_fake(FAKE_TITLES, pid)
            fake_domain = pick_fake(FAKE_DOMAINS, pid)

            cursor.execute(
                """
                UPDATE person_company
                SET company_name = ?, title = ?, domain = ?
                WHERE person_id = ? AND domain = ?
                """,
                (fake_company_name, fake_title, fake_domain, pid, pc_row["domain"]),
            )

        # ====================================================================
        # Step 5: Anonymize person_role_levels
        # ====================================================================
        print("Anonymizing person_role_levels...")
        cursor.execute(
            """
            SELECT person_id, seq
            FROM person_role_levels
            WHERE person_id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        prl_rows = [dict(row) for row in cursor.fetchall()]

        for prl_row in prl_rows:
            pid = prl_row["person_id"]
            fake_title = pick_fake(FAKE_TITLES, pid)
            fake_employer = pick_fake(FAKE_EMPLOYERS, pid)

            cursor.execute(
                """
                UPDATE person_role_levels
                SET title = ?, employer = ?
                WHERE person_id = ? AND seq = ?
                """,
                (fake_title, fake_employer, pid, prl_row["seq"]),
            )

        # ====================================================================
        # Step 6: Rebuild companies (do NOT try to anonymize in place)
        # --------------------------------------------------------------------
        # This MUST run after Step 4, which already rewrote person_company.domain
        # to synthetic *.example values. We fully REPLACE the companies table —
        # dropping every real firm row — and re-insert one synthetic row per
        # distinct domain now referenced by person_company. That (a) guarantees
        # no real firm name/domain survives, and (b) keeps the
        # person_company.domain -> companies.domain join intact so company pages
        # and firmographics still render. Deterministic: keyed off the domain
        # string, no hash().
        # ====================================================================
        print("Rebuilding companies (synthetic, join-consistent)...")
        cursor.execute("DELETE FROM companies")
        cursor.execute(
            """
            SELECT DISTINCT domain, company_name
            FROM person_company
            WHERE person_id IN ({}) AND domain <> ''
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        synthetic_firms = cursor.fetchall()

        for domain, company_name in synthetic_firms:
            cursor.execute(
                """
                INSERT OR REPLACE INTO companies
                    (domain, name, industry, industry_v2, size, employee_count,
                     company_type, ticker, founded, hq_location, linkedin_url,
                     summary, tags, likelihood, matched)
                VALUES (?, ?, ?, ?, ?, ?, ?, '', NULL, '', '', ?, '', NULL, 1)
                """,
                (
                    domain,
                    company_name or "Sample Capital Partners",
                    "Financial Services",
                    "Financial Services",
                    "51-200",
                    120,
                    "private",
                    "A synthetic firm used in the open-source sample dataset.",
                ),
            )

        # ====================================================================
        # Step 7: Anonymize news_curated
        # ====================================================================
        print("Anonymizing news_curated...")
        cursor.execute(
            """
            SELECT id, person_id
            FROM news_curated
            WHERE person_id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        news_rows = [dict(row) for row in cursor.fetchall()]

        for i, news_row in enumerate(news_rows):
            cursor.execute(
                """
                UPDATE news_curated
                SET headline = ?, summary = ?, source_url = ?,
                    source_host = 'example.com'
                WHERE id = ?
                """,
                (
                    f"Sample news headline {i % 5}",
                    f"Sample news summary about company and person activities",
                    sanitize_url(""),
                    news_row["id"],
                ),
            )

        # ====================================================================
        # Step 8: Anonymize person_insights identifying columns
        # ====================================================================
        print("Anonymizing person_insights...")
        cursor.execute(
            """
            SELECT person_id
            FROM person_insights
            WHERE person_id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        pi_rows = [dict(row) for row in cursor.fetchall()]

        for pi_row in pi_rows:
            pid = pi_row["person_id"]
            fake_company = pick_fake(FAKE_COMPANIES, pid)
            fake_domain = pick_fake(FAKE_DOMAINS, pid)

            cursor.execute(
                """
                UPDATE person_insights
                SET first_employer = ?, current_industry = ?,
                    job_function = '', employer_domain = ?
                WHERE person_id = ?
                """,
                (fake_company, "sample_industry", fake_domain, pid),
            )

        # ====================================================================
        # Step 9: Anonymize person_geo
        # ====================================================================
        print("Anonymizing person_geo...")
        cursor.execute(
            """
            SELECT person_id
            FROM person_geo
            WHERE person_id IN ({})
            """.format(
                ",".join("?" * len(sample_ids))
            ),
            sample_ids,
        )
        geo_rows = [dict(row) for row in cursor.fetchall()]

        for geo_row in geo_rows:
            pid = geo_row["person_id"]
            fake_city = pick_fake(FAKE_CITIES, pid).split(",")[0]
            # Use approximate center of Texas for all
            lat = 31.968599 + (pid % 100) * 0.001
            lng = -99.901810 + (pid % 100) * 0.001

            cursor.execute(
                """
                UPDATE person_geo
                SET city = ?, lat = ?, lng = ?
                WHERE person_id = ?
                """,
                (fake_city, lat, lng, pid),
            )

        # ====================================================================
        # Step 10: Delete non-sample people and dependent rows
        # ====================================================================
        print(f"Deleting non-sample rows...")
        keep_ids_str = ",".join("?" * len(sample_ids))

        # Delete from dependent tables first
        for table in [
            "person_insights",
            "news_curated",
            "person_geo",
            "person_company",
            "person_role_levels",
            "claims",
        ]:
            try:
                cursor.execute(f"DELETE FROM {table} WHERE person_id NOT IN ({keep_ids_str})", sample_ids)
            except sqlite3.OperationalError:
                pass  # Table might not exist

        # Finally delete people themselves
        cursor.execute(
            f"DELETE FROM people WHERE id NOT IN ({keep_ids_str})", sample_ids
        )

        # Clear truly orphaned / non-critical tables (no person_id or can be reconstructed)
        # These are not needed for rendering and are hard to anonymize
        for table in [
            "person_vectors",
            "identity_candidates",
            "person_sources",
            "batch_status",
            "geocode_cache",
        ]:
            try:
                cursor.execute(f"DELETE FROM {table}")
            except sqlite3.OperationalError:
                pass  # Table might not exist

        # ====================================================================
        # Step 11: Regenerate insights_snapshot
        # ====================================================================
        print("Regenerating insights_snapshot...")
        snapshot_data = compute_snapshot(cursor, sample_ids)
        cursor.execute("DELETE FROM insights_snapshot")
        cursor.execute(
            """
            INSERT INTO insights_snapshot
            (snapshot_year, generated_at, people_total, enriched_count, coverage,
             is_sample, narrative, payload)
            VALUES (?, datetime('now'), ?, ?, ?, 1, ?, ?)
            """,
            (
                2026,
                len(sample_ids),
                len(sample_ids),  # All sample people are "enriched"
                1.0,
                "Sample database for open-source release.",
                json.dumps(snapshot_data),
            ),
        )

        # Commit all changes
        conn.commit()
        conn.close()

        # ====================================================================
        # Step 12: Convert to read-only safe (DELETE journal mode)
        # ====================================================================
        print("Converting to read-only safe (DELETE journal mode)...")
        conn = sqlite3.connect(temp_db_path)
        cursor = conn.cursor()

        # Checkpoint any pending WAL
        cursor.execute("PRAGMA wal_checkpoint(RESTART)")
        # Switch to DELETE mode
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("VACUUM")

        conn.commit()
        conn.close()

        # Remove WAL/SHM sidecars if they exist
        for sidecar in [f"{temp_db_path}-wal", f"{temp_db_path}-shm"]:
            if os.path.exists(sidecar):
                os.remove(sidecar)
                print(f"Removed sidecar: {sidecar}")

        # ====================================================================
        # Step 13: Move to final location
        # ====================================================================
        shutil.move(temp_db_path, output_db_path)
        print(f"\nSuccess! Sample DB written to: {output_db_path}")

        # Verify
        verify_sample_db(output_db_path, sample_ids)

    except Exception as e:
        # Clean up temp file on error
        if os.path.exists(temp_db_path):
            os.remove(temp_db_path)
        raise


def compute_snapshot(cursor: sqlite3.Cursor, sample_ids: List[int]) -> Dict[str, Any]:
    """
    Compute aggregates for insights_snapshot payload.

    Returns the payload dict matching the schema in web/lib/db.ts.
    """
    # Landing firms (from current_employer claims)
    cursor.execute(
        """
        SELECT TRIM(value) as company, COUNT(*) as count
        FROM claims
        WHERE person_id IN ({}) AND claim_type = 'current_employer'
          AND TRIM(value) <> ''
        GROUP BY TRIM(value)
        ORDER BY count DESC
        LIMIT 10
        """.format(
            ",".join("?" * len(sample_ids))
        ),
        sample_ids,
    )
    landing_firms = [
        {"company": row[0], "count": row[1]} for row in cursor.fetchall()
    ]

    # Current titles (from current_title claims)
    cursor.execute(
        """
        SELECT TRIM(value) as title, COUNT(*) as count
        FROM claims
        WHERE person_id IN ({}) AND claim_type = 'current_title'
          AND TRIM(value) <> ''
        GROUP BY TRIM(value)
        ORDER BY count DESC
        LIMIT 10
        """.format(
            ",".join("?" * len(sample_ids))
        ),
        sample_ids,
    )
    current_titles = [
        {"title": row[0], "count": row[1]} for row in cursor.fetchall()
    ]

    # Seniority distribution
    cursor.execute(
        """
        SELECT peak_level, COUNT(*) as count
        FROM person_insights
        WHERE person_id IN ({})
        GROUP BY peak_level
        ORDER BY count DESC
        """.format(
            ",".join("?" * len(sample_ids))
        ),
        sample_ids,
    )
    seniority = [{"tier": row[0] or "Unknown", "count": row[1]} for row in cursor.fetchall()]

    # Signature stats
    cursor.execute(
        """
        SELECT COUNT(*) as total,
               SUM(CASE WHEN on_buy_side = 1 THEN 1 ELSE 0 END) as buy_side,
               SUM(CASE WHEN founder_partner = 1 THEN 1 ELSE 0 END) as founders,
               SUM(CASE WHEN reached_senior_leadership = 1 THEN 1 ELSE 0 END) as senior,
               ROUND(AVG(years_to_senior_leadership)) as avg_years_to_senior
        FROM person_insights
        WHERE person_id IN ({})
        """.format(
            ",".join("?" * len(sample_ids))
        ),
        sample_ids,
    )
    stats_row = cursor.fetchone()
    total = stats_row[0] or 1
    buy_side_count = stats_row[1] or 0
    founders_count = stats_row[2] or 0
    senior_count = stats_row[3] or 0
    avg_years = stats_row[4] or 8

    signature_stats = [
        {
            "label": "On Buy-Side",
            "value": f"{buy_side_count}",
            "detail": f"{int((buy_side_count / total) * 100)}% of alumni",
            "pct": int((buy_side_count / total) * 100),
            "key": "buy_side",
        },
        {
            "label": "Founders / Partners",
            "value": f"{founders_count}",
            "detail": "Building their own platforms",
            "pct": int((founders_count / total) * 100),
            "key": "founders",
        },
        {
            "label": "Senior Leadership",
            "value": f"{senior_count}",
            "detail": f"Reach in ~{avg_years} years",
            "pct": int((senior_count / total) * 100),
            "key": "senior",
        },
    ]

    # Landing sectors (from current_sector)
    cursor.execute(
        """
        SELECT current_sector, COUNT(*) as count
        FROM person_insights
        WHERE person_id IN ({}) AND current_sector IS NOT NULL
          AND current_sector <> ''
        GROUP BY current_sector
        ORDER BY count DESC
        LIMIT 10
        """.format(
            ",".join("?" * len(sample_ids))
        ),
        sample_ids,
    )
    landing_sectors = [
        {"sector": row[0], "count": row[1]} for row in cursor.fetchall()
    ]

    return {
        "landing_firms": landing_firms,
        "current_titles": current_titles,
        "seniority": seniority,
        "signature_stats": signature_stats,
        "landing_sectors": landing_sectors,
        "founders_partners": founders_count,
    }


def verify_sample_db(output_db_path: str, sample_ids: List[int]) -> None:
    """Verify the sample DB is correct and read-only safe."""
    if not os.path.exists(output_db_path):
        print(f"ERROR: Output file does not exist: {output_db_path}")
        return

    conn = sqlite3.connect(output_db_path)
    cursor = conn.cursor()

    # Check journal mode
    cursor.execute("PRAGMA journal_mode")
    journal_mode = cursor.fetchone()[0]
    print(f"Journal mode: {journal_mode}")
    if journal_mode != "delete":
        print("WARNING: Journal mode is not 'delete' — may not be read-only safe")

    # Check people count
    cursor.execute("SELECT COUNT(*) FROM people")
    people_count = cursor.fetchone()[0]
    print(f"People in sample: {people_count} (expected {len(sample_ids)})")

    # Check claims count
    cursor.execute("SELECT COUNT(*) FROM claims")
    claims_count = cursor.fetchone()[0]
    print(f"Claims in sample: {claims_count}")

    # Check snapshot
    cursor.execute("SELECT COUNT(*) FROM insights_snapshot")
    snapshot_count = cursor.fetchone()[0]
    print(f"Snapshots in sample: {snapshot_count}")

    if snapshot_count > 0:
        cursor.execute("SELECT payload FROM insights_snapshot LIMIT 1")
        payload_str = cursor.fetchone()[0]
        try:
            payload = json.loads(payload_str)
            print(f"Snapshot payload keys: {list(payload.keys())}")
        except json.JSONDecodeError as e:
            print(f"ERROR: Snapshot payload is not valid JSON: {e}")

    # Check for obvious real names (spot check)
    cursor.execute(
        "SELECT DISTINCT full_name FROM people WHERE full_name NOT LIKE '%-%' LIMIT 5"
    )
    suspicious_names = cursor.fetchall()
    if suspicious_names:
        print(f"Note: Found {len(suspicious_names)} names without hyphens (may be real)")

    conn.close()
    print("\nVerification complete!")


# ============================================================================
# CLI
# ============================================================================


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    source_db = sys.argv[1]
    output_db = sys.argv[2] if len(sys.argv) > 2 else "web/data/sample.db"

    generate_sample_db(source_db, output_db)


if __name__ == "__main__":
    main()
