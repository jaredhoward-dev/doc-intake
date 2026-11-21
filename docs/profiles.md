# Writing a profile

A profile is a JSON file that tells the engine which fields it may take out of one kind of document, and how to
find each one. Nothing outside a profile's fields is ever returned, so the profile is also the allowlist: the
fields it names are the only values the assistant can ever read from that kind of document.

Three example profiles live in `profiles/`: `receipt.json`, `invoice.json` and `utility_bill.json`. Copy them as a
start. This guide uses the receipt and the invoice as the worked examples; the utility bill shows a layout they
don't (see "The utility bill example" below). The code that reads profiles is `doc_intake/fields.py`; every key below is one it
reads.

## Pointing the engine at your profiles

A host calls `configure()` once at start-up:

```python
import doc_intake

doc_intake.configure(
    root=lambda: "/path/to/records",
    profiles_dir="/path/to/profiles",        # a folder of <name>.json profiles
    senders_file="/path/to/senders.json",    # optional known-senders map
)
```

`fields --task receipt FILE...` then loads `<profiles_dir>/receipt.json`. Profile names are lower case letters,
digits and underscores, and the file name must match the profile's `"name"`.

The known-senders map is not a profile. It can sit beside the profiles (as `profiles/senders.json` does) or
anywhere else. The engine never lists or loads the file `senders_file` names as a profile, whatever it is
called, and `fields --task` refuses it by name.

## The top level

| Key | Read by | What |
| --- | --- | --- |
| `name` | engine | The profile's name. Must match the file name: task files are named after it. |
| `description` | people | What the profile is for, in a sentence or two. The engine doesn't read it. |
| `never` | people | What the profile must never take (card numbers, names, addresses). The engine doesn't read it; the detectors and the allowlist enforce it. Write it so a reviewer can check the fields against it. |
| `groups` | engine | The groups fields belong to (see Groups). At least one. |
| `default_group` | engine | The group a page starts in. Defaults to the first group. |
| `fields` | engine | The fields (see Fields). |
| `tables` | engine | Optional line-item tables (see Tables). |

A task file records the sha256 of the profile it was made with, so you can tell later which version made it.

## Fields

Each field is one value the engine may take. From the receipt:

```json
{
  "group": "receipt",
  "name": "total",
  "type": "money",
  "labels": ["grand total", "amount paid", "total paid", "order total", "amount charged", "total"],
  "not": ["subtotal", "sub total", "sub-total", "savings", "items"],
  "required": true
}
```

- `group`, `name`: where the value is stored. The pair must be unique in the profile.
- `type`: the shape the value must have (see Value types). An unknown type is an error when the profile loads.
- `labels`: the words that introduce the value on the page. Earlier labels win over later ones when both match,
  so put the specific ones first ("grand total" before "total").
- `not`: words that, between the label and its value, mean this is the wrong line. "total" matches the
  "Subtotal" line too, so `not` throws that line out.
- `required`: the preview and `status` report a document missing this field. It doesn't stop extraction.

### How a value is found

For each label on a line, the engine looks for a value of the field's type:

1. after the label on the same line (`Total    $22.14`). When several labels sit on one line as columns (two or
   more spaces apart), each label owns the text up to the next one;
2. otherwise below the label, in the same columns, within the next three non-empty lines (boxes on forms).

Labels match whole words, case-insensitively, with any punctuation or spacing between their words. If no label at
all matches a document, the engine takes nothing from it: the document doesn't fit the profile.

Every value is then checked against the sensitive-value detectors. A value that overlaps a detected card,
account or ID number, or that looks like one when read with its label, is dropped and counted, never kept.

Every value with a spot on the page is also checked against its whole line, which matters where a rule reads only a
slice of one (the column below a label, or left of a `column_before` header). When the slice's edge fell inside a
longer number, the figure is no value and the field takes its next candidate or is missing. A figure is a piece of a
longer number when, at its first or last digit, it is glued to:

- a digit, or a dot and a digit (`...5678.00` out of `12345678.00`);
- a comma and a thousands group, three digits and no fourth, beside a run of one to three digits that follows no dot
  (`...,567.89` out of `1,234,567.89`; `12.50,13.00` is two figures);
- a dash or slash and a run of four or more digits that is not a whole date (`5678.00` out of `1234-5678.00`; `12.50/3.00`,
  `Invoice 12345-1` and the range `01/01/2030-01/14/2030` keep their figures).

A figure of any type but `date` that takes part of a date on its line is a piece too, so a `year` field never takes
`2030` out of `12/31/2030`, and nothing takes `01-14` out of `2030-01-14`. A minus before an amount (`-5.00`) and a
`$` start are a figure's own. Free-text types (`org_name`, `short_text`, `plan_name`) are taken as whole cells and
skip this check.

### Labels with an index

A label can be a pair `[text, index]` instead of a plain string:

- a number takes that value on the line, counting from 0. The invoice reads a billing period this way:
  `["billing period", 1]` is the second date after "Billing period";
- `"last"` takes the last value on the line (a total after a rate);
- `"current"` takes the second-to-last value, `"ytd"` the last (pay stubs: this period, then year to date);
- `"before"` takes the value just before the label;
- `"line"` owns the rest of the line, past any other labels on it. With no value on its line, it takes the
  nearest value within two non-empty lines above or below (an e-signature stamp pushes a signature's date onto
  its own line), but never from or past another `"line"` label's line. A `"line"` label is refused in a
  `per_plan` group: the value belongs to no plan.

### Options for any field

| Key | What |
| --- | --- |
| `anywhere` | When no label gives a value, take the first value of the type anywhere in the text. `"earliest"` (dates) takes the earliest date instead. |
| `document_date` | This is the date the document is about. Renewal, expiry and next-bill dates, and dates over 60 days ahead, are passed over. |
| `exclusive_label` | The field claims only its own whole label: the invoice's `invoice_number` label "invoice" never takes what follows "invoice date". |
| `label_at_cell_start` | A label counts only at the start of a cell and followed by a colon, a column gap or the end of the line. Stops "from" matching inside a sentence. |
| `from: "top"` | Also try the first cell of the first eight lines of page 1 (a business name at the top of a receipt). |
| `known_vendor` | Also look the document up in the known-senders map (see below). `true` uses every entry; a string such as `"lender"` uses only entries of that `kind`. |
| `range` | `0` or `1`: the start or end of a labeled date range ("March 1 - March 31, 2030"). Preferred over a single date after the label. |
| `link_month` | `0` or `1` (dates): the first or last day of the month a link's query names (`?year=2030&month=3`). |
| `name_date` | (dates) When nothing else gave a value, the `YYYY-MM-DD` at the start of the file name. |
| `requires` | `{"other_field": ["value", ...]}`: keep this field only when another field has one of those values. |
| `before` | Take the value just before the label, for every label of the field. |
| `each` | Keep every matching line as its own row, not only the first. |
| `column_before` | `"<header label>"` or `[label, n]`: ignore everything at or right of that header's column. |
| `below_lines` | Look up to this many non-empty lines below the label instead of three. |
| `below_values_only` | With `below_lines`: a line below counts only when it holds nothing but values. |
| `box_row` | `true`: the value is one figure in a row of boxes under wrapped headers (a scanned Truth in Lending disclosure). The figures line (two or more cells, all values) and the lines just above it are split into columns by the widest header line; the field takes the figure in the column whose header text holds its label. A blank box, two figures in one box, or a figure astride a column edge gives nothing, never a neighbour's figure. Also allowed on a group, for all its fields. |
| `model_from` | Take what follows a make in another field's value (needs a type registered with `model_prefix`). |

The examples use `anywhere`, `document_date`, `exclusive_label`, `label_at_cell_start`, `from`,
`known_vendor`, `range`, `link_month` and `name_date`. The rest are for forms with fixed columns and are described
at the top of `fields.py`.

## Value types

| Type | Matches |
| --- | --- |
| `money` | `$1,234.56`, `12.00`, up to four decimals |
| `usage` | a meter reading with its unit: `1,234 kWh`, `56.7 therms`, `12 CCF`, `4,500 gallons`, `300 cubic feet` (also MWh, HCF, MCF, Dth, kgal, gal, cu ft, m3); stored as the figure, one space and the unit (`1234kwh` gives `1234 kWh`) |
| `percent` | `3.875 %` |
| `date` | `03/14/2030`, `2030-03-14`, `Mar 14, 2030`; stored as `YYYY-MM-DD` |
| `currency` | `USD`, `EUR`, `GBP`, ...; an `anywhere` currency field with no other value falls back to USD when the text has a `$` |
| `pay_method` | card names, cash, check, ACH, ...; stored as `card`, `cash`, `check`, `ach`, `zelle`, `money-order` or `other` |
| `ref` | upper-case letters, digits and dashes, with at least one digit (`DP-20300401`) |
| `year`, `count` | a four-digit year; a small whole number |
| `org_name` | a business name, taken only as a whole cell, trimmed where it runs into "Bill to" and similar |
| `short_text`, `plan_name` | short free text, taken only as a whole cell |

Free-text types (`org_name`, `short_text`, `plan_name`) never hold three or more digits in a row, so street
numbers, ZIP codes and IDs stay out. A `usage` figure has at most six digits (nine with thousands commas) and no
leading zero, and is never the tail of a longer number (a digit, comma, dot, dash, slash or `#` just before it, or a
group of digits a space before it), checked against the page itself, so an account or meter number printed beside a
unit word is never a reading. Like every type's, a `usage` value is still checked against the detectors. `fields.py` lists the rest (`cost`, `limit`, `duration`, `policy_type`, ...).
A host adds its own types with `fields.register_type(name, pattern)` before loading a profile that uses them. They get
the same whole-line check as the built-in types (see "How a value is found").

## Groups

Every field belongs to a group, and `groups` must name each one. Each example has one group with
`"global": true`, which is all a single-section document needs:

```json
"default_group": "receipt",
"groups": {"receipt": {"global": true}}
```

A field's labels are looked for only while its group is the current section, or everywhere when the group is
`global`. For documents with sections, a group can also have:

- `words`: a line whose cell holds one of these words starts that group's section;
- `per_plan` and `plan_markers`: a benefits-style table, where a heading row names several plans and each later
  row gives one value per plan;
- `box_row`: every field of the group is read as a figure in a row of boxes (see `box_row` under Fields).

## Tables

`tables` reads line-item tables: a header row of column labels, then one row per line below it.

```json
{"group": "items", "header_min": 3, "row_requires": ["code"],
 "columns": [{"name": "code", "type": "ref", "labels": ["code"]}, ...]}
```

A header line needs at least `header_min` (default 3) column labels. Each cell goes to the nearest column, and a
row with none of the `row_requires` columns ends the table.

## The known-senders map

`profiles/senders.json` is an example map with made-up vendors on `.test` domains. A field with `known_vendor`
uses it to name the sender cleanly, ahead of anything read from the page.

```json
{
  "name": "Example Cloud Marketplace",
  "domains": ["marketplace.example-cloud.test"],
  "patterns": ["\\bexample\\s+cloud\\s+marketplace\\b"],
  "header_lines": 15
}
```

- `vendors` are checked in order and the first match wins: put a specific sender before a general one that would
  also match (the marketplace before `example-cloud.test`).
- `domains` match as whole domain names anywhere in the text; `patterns` are case-insensitive regular expressions.
- `header_lines`: look only at the first n lines of page 1.
- `kind`: a tag such as `"lender"`. A field with `"known_vendor": "lender"` uses only entries of that kind; a field
  with `true` uses every entry.
- `capture`: take the name from the pattern's first group instead of `name` (a pattern for a family of names).

Two optional sections cover senders no list can name:

- `stripe`: when no vendor matched and page 1 matches one of `markers`, the business name is the cell before the
  `bill_to` pattern (or the line above it). Hosted invoices put it there.
- `events`: when page 1 matches one of `markers`, the event's heading (a line matching `heading`) is the sender.

Neither runs for a field asking for a `kind`.

A host may keep its own keys in a vendor entry (a filing suggestion, say) and read them with
`fields.sender_entry(name)`. The engine ignores them, so the example map has none.

## The utility bill example

`utility_bill.json` takes the provider, the statement date, the service period, the due date, the amount due, the
previous balance, the payments received and the usage with its unit. Its `never` list (account numbers, service
addresses, customer names, meter numbers) has no field that could hold any of them, and the detectors drop an account
number that lands where a field's value goes. It shows four things the first two examples don't:

- **Values below labels.** Many bills print a row of labels with the values on the line below
  (`Bill Date   Due Date`, then the dates). The same profile reads that layout and the labels-and-values-on-one-line
  one; `tests/test_example_profiles.py` checks both.
- **Separate start and end labels.** `period_start` and `period_end` take "Service From" and "Service To" as labels
  of their own, besides the `range` and indexed labels (`["service period", 1]`) the invoice uses.
- **`not` and a row of labels.** When a label's value sits below it, `not` is checked against the rest of the
  label's line, the neighbouring column labels included. A `not` word that is another field's label
  (`"due date"` on the statement date) then throws out the whole header row. The utility bill relies on longer
  labels winning instead: "Due Date" belongs to `due_date`, so the statement date's `"date"` label never matches
  inside it.
- **Usage with its unit.** The `usage` field (type `usage`) takes `742 kWh` or `4,500 gallons`, never a bare figure.
  It sets `label_at_cell_start`, so "usage" counts only at the start of a cell: "Average Daily Usage" and "Previous
  Usage" never give the bill's usage. It has no `not` list, since a `not` word anywhere in a row of labels would
  throw out the whole row.

## Checking a profile

- Load it: `fields.load_profile("receipt")` raises `ProfileError` on an unknown type, an unknown group or a
  duplicate field.
- Try it on fake text: `fields.extract(profile, [(1, text)], "fake.txt")` returns the records, the dropped counts
  and any plans. `tests/test_example_profiles.py` does this for every example.
- On a real document, `fields --task NAME --labels FILE` shows which labels matched where, never a value. A
  detector hit inside an email header names the header only when it is a standard one (`From`, `Received`, ...);
  any other name shows as `other`.
