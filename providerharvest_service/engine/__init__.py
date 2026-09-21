"""CKAN-agnostic provider-harvest engine: transports, auth strategies,
envelope-encrypted secrets, field mapping, SSRF-safe network validation,
response parsers, and CKAN-DataStore/resource loaders (the latter two
via dependency-injected callables, not a direct CKAN import).

Ported verbatim from ckanext-providerharvest's own equivalent modules --
that plugin's copies were already framework-agnostic (no ``ckan`` import
at all, only ``ckanext.providerharvest.*`` siblings), which is what made
splitting the execution engine out into this standalone service
straightforward instead of a rewrite. See the top-level service/README.md
for the split's rationale and what still talks to CKAN, and how.
"""
