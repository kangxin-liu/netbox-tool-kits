# Product overview

## Purpose

`netbox-tool-kits` is an internal Django web application that lets data-centre engineers
populate a NetBox instance from spreadsheets instead of clicking through the NetBox UI or
hand-writing API scripts. It talks to NetBox over the REST API using a URL and API token
supplied through environment configuration.

The tool is a **write-side helper for NetBox**, not a source of truth. NetBox remains the
system of record; this app owns nothing but its own audit trail of what it submitted.

## Target users

Data-centre / network engineers commissioning new halls, racks, devices, and cabling. They
are trusted, authenticated internal staff — not the public, and not customers.

## Iteration 1 scope: create only

The first iteration deliberately supports **creation of new entries only**. Three importers,
one page each, reachable from a shared nav bar:

1. **Site & rack hierarchy** — region, datacenter (site), datahall (room), racks.
2. **Devices** — placed into racks by face and rack unit.
3. **Cabling** — device-to-device, device-to-panel, and panel-to-panel links.

Each page offers a downloadable CSV/XLSX template, accepts an upload of the same shape, validates it
in full, previews what would change, and only then — if validation found no blocking problem — allows
a write to NetBox.

## Explicitly out of scope for iteration 1

- Updating or deleting existing NetBox objects (creation and idempotent skip only).
- IPAM (prefixes, IPs, VLANs), virtualization, circuits, power path modelling.
- SSO / multi-user roles — a single local Django superuser is the only identity.
- Background workers, queues, scheduled syncs, webhooks.
- Multi-NetBox-instance switching from the UI.

## Product principles

- **Validation is a hard gate, not advice.** A file is validated in full before anything is written,
 and a single blocking error makes the whole file unappliable — there is no partial-apply path, even
 on request. A half-populated NetBox is the failure this tool exists to prevent.
- **Block on what changes the outcome; warn on the rest.** A problem blocks when proceeding could
 produce wrong data, and warns when it cannot. Warning on something that matters is how imports go
 wrong; blocking on something that does not is how a gate loses the operator's trust and gets
 worked around. Both failures are real, so each new check has to argue for its severity.
- **Fail loudly, and say exactly where.** Every issue names the file, worksheet, spreadsheet line,
 column, what was received, and what was expected. The engineer should never have to read a stack
 trace, or guess which row the tool meant, to fix a spreadsheet.
- **Idempotent re-runs.** Re-uploading the same sheet must not create duplicates; already
 present objects are reported as skipped.
- **The template is the contract.** The downloadable template and the parser are generated
 from one shared column definition so they cannot drift apart.
- **Boring and small.** Server-rendered Django pages, no SPA, no build step.

## Definition of success

An engineer can commission a new datahall — hierarchy, ~40 racks, a few hundred devices, and
its cabling — from three spreadsheets, with every rejected row explained well enough to fix
without asking for help.
