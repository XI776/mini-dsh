# Text helpers

`util.text.normalize` is used by three callers:

- `report.render_title` lowercases titles before they are compared.
- `importer.import_row` normalizes the `name` column of imported rows.
- `cli.describe` normalizes the search term typed by an operator.

`normalize(value, keep_case=False)` must:

- strip leading and trailing whitespace,
- collapse every run of internal whitespace (spaces, tabs, newlines) into one
  space,
- lowercase the result unless `keep_case` is true,
- raise `ValueError` for non-string input.

`keep_case` must be accepted as a keyword argument; existing positional calls
stay valid. The importer is the only caller that needs the new behaviour: it
must preserve the case of imported names, while every other caller keeps
lowercasing.
