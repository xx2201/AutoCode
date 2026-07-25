# Web fetch tool

AutoCode always registers the read-only `web_fetch` tool. It accepts a public
HTTP(S) URL and an extraction prompt, then returns bounded readable text to the
model.

Safety behavior:

- upgrades `http://` URLs to `https://`;
- rejects URL credentials, localhost, private/link-local IPs, cloud metadata
  hostnames, and direct access to non-public IP literals;
- reports redirects instead of following them silently;
- accepts only text, HTML, JSON, and XML responses;
- reads at most 2 MB and returns at most 50,000 characters;
- caches a fetched URL in memory for 15 minutes;
- requires manual policy approval for every Agent call.

Some desktop proxy products resolve public hostnames into the IANA benchmarking
range `198.18.0.0/15`. AutoCode permits that range only when DNS returns it for
a hostname; direct URLs targeting the range remain blocked.

`web_fetch` does not execute page scripts or write workspace files. Fetched
content remains untrusted reference material and cannot override user or system
instructions.
