# Supabase PostgreSQL CA

`supabase-prod-ca-2021.crt` is a **public CA certificate**, not a private key or
application credential. It is used with PostgreSQL `sslmode=verify-full` so both
the issuing CA and the server hostname are checked.

Source: the Supabase Database Settings “Download certificate” link,
[official certificate](https://supabase-downloads.s3-ap-southeast-1.amazonaws.com/prod/ssl/prod-ca-2021.crt).
See [Supabase SSL enforcement](https://supabase.com/docs/guides/platform/ssl-enforcement).

- Issuer: Supabase Root 2021 CA
- Valid through: 2031-04-26 10:56:53 UTC
- SHA-256 certificate fingerprint:
  `807025AD50D4ED219D2C9C7D299C004F824EB00CF7F65AFEF607D07B72E6CAFA`

Set `sslrootcert` to the absolute path of this file in the environment where the
connection runs. In the Docker image this is
`/app/backend/certs/supabase-prod-ca-2021.crt`. A local migration needs its local
absolute path instead. Keep the password-bearing `DATABASE_URL` out of Git,
logs, command arguments, browser code, and screenshots.

If the certificate changes or expires, obtain the replacement from the official
dashboard and verify its origin. Do not work around validation failures by
turning off certificate or hostname verification.
