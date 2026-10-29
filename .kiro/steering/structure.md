# Project structure

## Layout

```
netbox-tool-kits/
├── .kiro/
│   ├── steering/                  # product.md, tech.md, structure.md (this file)
│   └── specs/
│       └── netbox-bulk-provisioning/
│           ├── requirements.md
│           ├── design.md
│           └── tasks.md
├── config/                        # Django project (settings, urls, wsgi)
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── importer/                      # the single Django app
│   ├── columns.py                 # ColumnSpec definitions — the sheet contract
│   ├── forms.py                   # upload forms (file validation lives here)
│   ├── models.py                  # ImportRun, ImportRowResult (audit only)
│   ├── urls.py
│   ├── views.py                   # thin: parse → service → render
│   ├── naming.py                  # opaque rack-name and NetBox slug validation
│   ├── importers/
│   │   ├── base.py                # shared validate → plan → apply pipeline
│   │   ├── hierarchy.py           # prepared site/parent → child location / rack
│   │   ├── devices.py             # devices into racks
│   │   └── cabling.py             # cables, incl. patch panels
│   ├── services/
│   │   ├── netbox_client.py       # pynetbox factory + version probe
│   │   ├── lookups.py             # cached natural-key lookups & choice validation
│   │   └── sheets.py              # csv/xlsx read + template generation
│   ├── templates/importer/        # base.html + one template per page
│   └── tests/
│       ├── fixtures/              # small synthetic sheets — no production data
│       ├── test_naming.py
│       ├── test_sheets.py
│       └── test_importers_*.py
├── static/
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Organising principles

- **One app.** `importer` holds everything domain-specific. Splitting into more apps is not
 justified at this size.
- **Columns defined once.** `columns.py` is the single source of truth for each sheet's
 columns, order, required-ness, and help text. Template generation, parsing, and validation
 all read from it, so a downloaded template can never disagree with the parser.
- **Importers share one pipeline.** Every importer implements the same three phases from
 `importers/base.py`:
 1. `validate(rows)` → per-row field/format errors, no network calls beyond lookups
 2. `plan(rows)` → an ordered list of intended creates plus "already exists" skips
 3. `apply(plan)` → executes creates, recording each outcome
 The preview page renders the plan; the confirm action runs `apply`.
- **No NetBox calls from templates or models.** Only `services/` and `importers/` touch the API.
- **Audit, don't mirror.** `models.py` records what was submitted and what came back. It never
 becomes a second copy of NetBox's inventory.

## Naming conventions

- Modules and functions: `snake_case`. Classes: `PascalCase`.
- Importer classes are named `<Domain>Importer` (`HierarchyImporter`, `DeviceImporter`,
 `CablingImporter`).
- Spreadsheet column keys are `snake_case` and match the template header text exactly, so a
 header can be mapped without a translation table. Header matching is case-insensitive and
 whitespace-trimmed.
- URL names are `importer:<page>` (`importer:hierarchy`, `importer:devices`, `importer:cabling`)
 plus `importer:<page>_template` for template downloads.
- Test files mirror the module under test: `importers/devices.py` → `tests/test_importers_devices.py`.

## Where things go

| Adding... | Goes in |
| --- | --- |
| a new spreadsheet column | `columns.py` — including its `Conflict` classification, which has no default so the choice cannot be skipped — then the relevant importer |
| a new NetBox object type | a new module under `importers/`, plus lookups in `services/lookups.py` |
| a new page | `views.py`, `urls.py`, a template, and a nav-bar entry in `base.html` |
| a naming-convention change | `naming.py` only — nothing else parses rack names |
| test data | `tests/fixtures/` as synthetic values only (`example.com`, fake serials) |
