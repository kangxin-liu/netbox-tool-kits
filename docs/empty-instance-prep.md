# Preparing an empty NetBox

Classification: INTERNAL. Nothing here holds a credential; the API token you create belongs in `.env`
or your secret manager, never in a commit.

A brand-new NetBox has no device roles, no manufacturers, no device types, and no patch panels.
Iteration 1 of this tool creates none of those — it creates the DCIM objects its three sheets
describe, and on the device sheet the loopback and management addresses assigned to those devices,
and nothing else — so on an unprepared instance the device and cabling importers have
nothing to resolve against. That is expected, and each page's readiness panel says so before you
upload anything.

What that means in practice:

| Sheet | On a brand-new instance |
| --- | --- |
| 1. Hierarchy | **works immediately.** It creates its own parents: region, site, location, racks |
| 2. Devices | needs device roles and device types to exist first, with `mgmt_only` and `loopback` marked on network types |
| 3. Cabling | needs devices that have ports, which means device types with port templates |

So a new instance is always usable for sheet 1 first. Everything below is preparation for sheets 2
and 3.

## 1. A local NetBox, if you need one

`netbox-docker` brings Postgres and Redis with it, so this is the whole dependency list.

```bash
git clone -b release https://github.com/netbox-community/netbox-docker.git
cd netbox-docker
cp docker-compose.override.yml.example docker-compose.override.yml
# The example maps 8000:8080. Keep 8000 — this app serves 8080, and they must not collide.
docker compose pull
docker compose up -d
docker compose exec netbox /opt/netbox/netbox/manage.py createsuperuser
```

NetBox comes up on `http://localhost:8000/` after a few minutes. Requirements: Docker 20.10.10+ and
`docker compose` 1.28+.

Then in NetBox, create an API token (**your profile → API Tokens**) and put it in this app's `.env`:

```
NETBOX_URL=http://localhost:8000/
NETBOX_TOKEN=<the token you just created>
```

`http://` is accepted **only** because the host is loopback, and the app shows a development banner
while pointed at it. A plaintext URL for any other host is refused at startup.

Check the version: this app supports NetBox `>=4.6,<4.7` and refuses to import outside that range
rather than failing partway through on a renamed field.

## 2. Device roles

Create every role your device sheet's `device_role` column names, plus the fallback role that
`NETBOX_DEFAULT_DEVICE_ROLE` points at (`unassigned` by default), which is what a row with no role
gets. **Organisation → Device Roles**, or the API:

```bash
curl -s -X POST http://localhost:8000/api/dcim/device-roles/ \
  -H "Authorization: Token $NETBOX_TOKEN" \
  -H "Content-Type: application/json" \
  -d '[{"name": "Unassigned", "slug": "unassigned"},
       {"name": "Leaf Switch", "slug": "leaf-switch"},
       {"name": "Spine Switch", "slug": "spine-switch"},
       {"name": "Patch Panel", "slug": "patch-panel"},
       {"name": "GPU Server", "slug": "gpu-server"}]'
```

The devices page's readiness panel reports whether the default role resolves and how many roles
exist, so you can confirm this without uploading anything. A role named in a sheet that does not
exist is a blocking error naming the missing role — the tool will not invent one.

## 3. Device types

Device types are templates: a device created from a type inherits that type's interfaces, front
ports, and rear ports. This is the step that decides whether sheet 3 has anything to cable.

The community library at [`netbox-community/devicetype-library`](https://github.com/netbox-community/devicetype-library)
carries YAML definitions for most vendors. Two ways in:

* **By hand, per model.** Open the model's YAML in the library
  (`device-types/<Manufacturer>/<Model>.yaml`), then in NetBox go to **Devices → Device Types →
  Import** and paste it. NetBox creates the manufacturer, the type, and all of its port templates in
  one go. Fine for the handful of models you actually use.
* **In bulk.** [`netbox-community/Device-Type-Library-Import`](https://github.com/netbox-community/Device-Type-Library-Import)
  walks the library and imports a chosen list of vendors over the API. Review what it will import
  first: importing every vendor leaves you with thousands of types to search through.

Three things to check on each type you import:

* **`u_height` must be right.** The device importer validates that a device fits inside its rack and
  does not overlap another, and it takes the height from the device type when the sheet omits it.
* **Ports must exist on the type.** A type with no interface templates produces devices with no
  interfaces, and every cabling row against them fails to resolve a port. NetBox does **not**
  retro-fit ports onto devices that already exist, so fix the type *before* running sheet 2.
* **Management and loopback must be marked, not merely present.** Sheet 2 assigns `management_ip4` /
  `management_ip6` to the unique interface template with `mgmt_only` set, and `loopback_ip4` /
  `loopback_ip6` to the unique template whose type is `loopback`. Both families of one role land on
  that same interface. It does not read interface names from the sheet, because Arista, Juniper and
  Nvidia Cumulus all name those ports differently. Community YAML often types `Loopback0` as
  `virtual`; that is **not** a loopback for this tool — change it to `loopback` on the type. A type
  with two `mgmt_only` interfaces is ambiguous and every management address against it will be
  rejected. A patch-panel type should have neither, so all four IP cells stay blank.

If a type you already created is missing its port templates, `scripts/seed_netbox.py` adds them to
existing types — interface templates for switches, front/rear port templates for panels. It is a dry
run by default, refuses a non-loopback target unless `--allow-remote` is passed, and skips anything
already present:

```bash
python3 scripts/seed_netbox.py                     # show the plan
python3 scripts/seed_netbox.py --apply             # write it
python3 scripts/seed_netbox.py --naming generic    # Ethernet1 instead of swp1
```

`--naming` must match whatever your cabling sheet's `a_port` / `b_port` columns say, or every cabling
row will fail to find its port.

## 4. At least one patch panel

Sheet 3 cables devices to panels, and a panel is an ordinary device in NetBox whose device type
defines front and rear ports with a mapping between them.

**NetBox 4.5 changed this format.** `FrontPort.rear_port` and `rear_port_position` were removed in
favour of a `PortMapping` model, exposed on the API as a `rear_ports` array. A front port template is
created like this:

```json
{
  "device_type": 12,
  "name": "F1",
  "type": "lc",
  "positions": 1,
  "rear_ports": [
    {"position": 1, "rear_port": 480, "rear_port_position": 1}
  ]
}
```

`rear_port` is the id of a rear port template on the **same** device type. The array is what makes a
breakout panel expressible: four LC front ports can each map to a different position of one MPO rear
port, which is why the rear port carries `positions: 4`.

`scripts/seed_netbox.py` builds three panel layouts in this format against existing panel device
types, and is the quickest way to get a working panel:

| Layout | Rear | Front |
| --- | --- | --- |
| 24-pair LC, 1:1 | `R1`–`R24` | `F1`–`F24`, each to its own rear port |
| 24-port MPO, 1:1 | `MPO1`–`MPO24` | `MPO1`–`MPO24`, same names on both faces |
| 8-group 4× breakout | `MPO1`–`MPO8`, 4 positions each | `1A`–`8D`, four per rear port |

The MPO panel is worth understanding before you write a cabling sheet: front and rear ports share
their names, which is exactly why the sheet has a `b_port_side` column. Without it, `MPO1` would be
ambiguous.

Once the type exists, create the panel device itself through sheet 2 like any other device, giving it
the panel's model in the `model` column.

### If you will use breakout cables, check the switch device types too

There are two places a fan-out can live, and they need opposite preparation. A fan-out inside a
**panel** is the `rear_ports` mapping above. A fan-out inside a **cable** is a cable profile, set from
the cabling sheet's `cable_profile` and `cable_group` columns — and for that the cable has to terminate
on the switch's physical cage.

Switch device types using the Cumulus/SONiC naming convention sometimes define both the cage and the
OS-level breakout children as interfaces: `swp1` typed QSFP-DD alongside `swp1s0` and `swp1s1`. Both
are cable-capable, so a sheet author has two plausible places to land the cable and they mean different
things. Prepare the type so there is only one:

* Keep `swp1` as the physical interface and put the cable there, with the profile describing the
  fan-out. This is the arrangement cable profiles are designed for and the one the cabling sheet
  documents.
* Make the `swpNsM` children **virtual interfaces with `parent` set to their cage**, or remove them.
  Left as physical interfaces they invite a second cable onto a port that is already consumed, and
  NetBox will report `swp1` as free while its children are cabled.

The importer will not fix this for you — it never modifies an object it did not create — so it is prep,
and it is worth doing before the first breakout sheet rather than after.

### Adding a template later does not backfill the devices already built from it

This is the one fact about device types that surprises everyone, and it is worth knowing now rather
than three sheets in: **NetBox copies a type's interface templates onto a device when the device is
created, and never again.** Add `swp49` to a type today and every switch built from it yesterday
still has no `swp49`. The type is right, the device list looks right, and the cabling sheet fails on
a port you can plainly see in the type's template list.

It follows that the order in section 3 is not a stylistic preference. Get the templates right
*before* running sheet 2, because the devices sheet 2 creates are the ones that inherit them.

If you find yourself on the wrong side of this, there are three ways out:

* Delete and re-create the affected devices, if the instance is still young enough that nothing is
  hanging off them. Cleanest, and usually only realistic on a lab.
* Add the missing interfaces in NetBox by hand, or by script.
* Turn on `NETBOX_AUTOCREATE_INTERFACES` and let the cabling sheet create them from the template as
  it goes. This exists precisely for this gap — see "When a port the sheet names does not exist in
  NetBox" in the operator runbook, including why it is off by default and what you are agreeing to
  when you acknowledge the warnings.

None of the three is prep, strictly: they are all repairs. Getting the templates in before sheet 2
avoids the question.

## 5. Confirm, then import

Load each importer page and read its readiness panel — it is strictly read-only and creates nothing:

| Page | Tells you |
| --- | --- |
| Hierarchy | that it needs no prerequisites |
| Devices | whether the default role resolves, how many roles and device types exist, and how many of those types define a management (`mgmt_only`) and a loopback interface |
| Cabling | how many devices exist, and how many interfaces and panel ports they have |

Then import in order: **sheet 1 → sheet 2 → sheet 3**, validating each in full before applying it.

`scripts/snapshot_netbox.py` writes a read-only snapshot of the instance's DCIM contents, which is
worth taking before a large import so that "what did this change" has an answer that does not depend
on memory.

## After a NetBox upgrade

Two things to re-check, because both were verified by measurement rather than assumed:

* `scripts/verify_bulk_atomicity.py` — confirms a rejected bulk create still stores nothing, which is
  what lets an apply submit one list per endpoint (decision D7).
* The supported range in `config/settings.py` (`NETBOX_MIN_VERSION`, `NETBOX_MAX_VERSION_EXCLUSIVE`).
  The app refuses an instance outside it deliberately: 4.2 renamed `Rack.type` and 4.5 replaced the
  front-port mapping, and either would otherwise fail mid-import.
