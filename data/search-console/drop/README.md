# Drop Search Console exports here

1. Open [Search Console](https://search.google.com/search-console), pick a property.
2. Go to **Performance**, set the date range you want (Last 3 months is a good default).
3. Press **Export → Download CSV**. You get a zip named after the property.
4. Drop the zip in this folder. Repeat for each property.
5. From the repo root, run:

       python3 scripts/import_gsc.py

6. Refresh the dashboard.

If a file's name does not contain the site's hostname, put it in a subfolder
named for the site instead, e.g. `gocheckmypet.com/Queries.csv`. The importer
will not guess.

Files here are inputs only; the importer writes `data/search-console.json`.
