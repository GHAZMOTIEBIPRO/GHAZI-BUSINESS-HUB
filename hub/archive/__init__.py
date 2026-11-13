"""GH DATA ENGINE — national procurement intelligence archive.

Separate from the opportunity radar (hub/radar.py): the radar holds *current, reviewed leads*; this package
holds the *historical archive* of tenders, issuers, companies, bids and awards with full provenance.

Rules enforced here (issue #2):
- every record keeps its source URL, the raw value as published, and the batch that wrote it;
- nothing is published while its source rights are unverified (`publish_allowed` stays False);
- a bid list is never assumed complete, a winner is never inferred from the lowest bid, and names are never
  merged into one legal entity without an official identifier;
- an awarded tender is an archive record, never a subcontract opportunity by itself.

The archive tables use their own metadata (`ArchiveBase`) managed by Alembic (migrations/), so the existing
`create_all` start-up of the app never touches them.
"""
