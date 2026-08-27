# Affiliate export drop

Put network CSV exports here, then run:

    python3 scripts/import_affiliates.py

Name each file (or a subfolder) for its network so the importer can label it:
`awin`, `impact`, `cj`. Anything else is still read and counted as "unknown".

What to export:

- **Awin**: Reports > Transactions, or the Clickref report. Keep the Click Ref and
  Click Ref 2 columns: Click Ref 2 is where the cross-site origin lives.
- **Impact**: Reports > Action Listing, or SubId performance. Keep SubId1 and SubId2.
- **CJ**: Reports > Commission Detail, or the SID report. CJ has only one SID, so a
  cross-site origin arrives joined to the placement with a `~`, and the importer
  splits it.

Files here are working material and are not deployed.
