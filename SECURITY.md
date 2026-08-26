# Security policy

Please do not open a public issue for a vulnerability that could expose source
documents, transcriptions, filesystem paths, or review data. Use GitHub's
[private vulnerability reporting][report-vulnerability] for the repository
instead.

Foresight OCR's review server is designed for local use and binds to
`127.0.0.1` by default. It does not provide authentication or authorization; do
not expose it directly to an untrusted network. The server rejects non-loopback
HTTP authorities, cross-site browser mutations, and non-JSON write requests as
defense in depth against DNS rebinding and local-service CSRF; these checks are
not a substitute for an authenticated network service.

[report-vulnerability]: https://github.com/runkaiz/foresight-ocr/security/advisories/new
