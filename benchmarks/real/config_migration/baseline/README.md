# Reporting service configuration

Operator configuration arrives as a plain dict (from a YAML file in production,
from a test fixture in CI) and is normalized by `app.config.load_config`
before `app.client.build_client` uses it.

Normalization rules:

- `timeout` – number of seconds. Accepts an `int` or a numeric `str`. The
  normalized value is always a positive `int`; anything else raises
  `ValueError`. Missing values fall back to `app.defaults.DEFAULT_TIMEOUT_S`.
- `retries` – retry budget. Accepts an `int` or a numeric `str` and is
  normalized exactly like `timeout`, with the same `ValueError` behaviour.
  Missing values fall back to `app.defaults.DEFAULT_RETRIES`.
- `base_url` – must be an `https://` URL. The normalized value never has a
  trailing slash. A missing value falls back to
  `app.defaults.DEFAULT_BASE_URL`; anything that is not an `https://` URL
  raises `ValueError`.

The normalized dict uses the keys `timeout_s`, `retries` and `base_url`.
