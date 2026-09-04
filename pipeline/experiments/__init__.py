"""Frozen experiments and one-off probes — NOT part of the production pipeline.

These are the bake-offs and probes that produced the decisions recorded in the
memory notes (news A/B, Perplexity agent vs search, Sonar probe, LinkedIn agent
and PDL probes, the original Firecrawl-only discover pass). They are kept for
reproducibility of those numbers, not maintained as features: no new code
should import from here, and their adapters (GNews, GDELT, the Perplexity
agent) are not wired into phase2_enrich.

Run one from the pipeline/ directory as a module so sibling imports resolve:

    python -m experiments.agent_bakeoff --limit 3
    python -m experiments.news_experiment --limit 25 --sources gdelt
"""
