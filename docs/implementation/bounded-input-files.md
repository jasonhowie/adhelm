# Bounded regular-file command inputs

The installed CLI reads typed JSON requests through `input_files.py` before
using their contents. JSON retains its 1 MiB byte limit. Local raster-image
analysis uses the same descriptor checks with its existing 20 MiB bound and
preserves the `CREATIVE_MEDIA_TOO_LARGE` diagnostic. Credential onboarding keeps
its separate private OAuth-client reader and credential-file restrictions.

An input must be a readable regular file. Initial directories, final-component
symbolic links, pipes, sockets and devices fail before descriptor opening. The
reader uses nonblocking/no-follow flags and then checks the opened descriptor,
so replacing a checked regular file with a pipe cannot make the command wait for
a writer. It requires platform support for those primitives; unsupported
platforms fail explicitly. Parent-directory aliases remain supported because
this is an explicitly selected ordinary input, not a private trusted directory.

The reader bounds each read and total bytes, checks selected/opened identities,
and verifies file size, modification/change timestamps and the final path again
before returning bytes. Observed replacement, removal, growth or rewriting
fails closed. The descriptor is closed on success and every failure. A final
path removed after opening reports `INPUT_FILE_CHANGED`; an initially missing
selection reports `INPUT_FILE_INVALID`. Errors contain fixed public messages,
without caller paths, OS exception text or input content. These observations do
not lock the filesystem, defeat privileged tampering or promise network/disk
filesystem latency.

JSON bytes go unchanged into the existing duplicate-key, nonfinite-number,
canonical-domain and closed-schema validation. Integer-looking floats and
booleans are never normalized into integers before model validation. The
inherited `StrictModel` version check also closes Pydantic's numeric equality
shortcut for declared `schema_version` fields, including output and retention
preferences nested in configuration. Each model still enforces its supported
version separately; valid existing canonical bytes remain unchanged.

The reader is used by all seven campaign/strategy compilers, provider report
requests, profile configuration, conversion definitions, measurement declaration
checks, creative manifests, pause and GA4 CREATE planning, retained GA4
candidates, and every typed artifact request. Existing read-only profile,
configuration and output-policy preconditions keep their ordering. Invalid
input cannot reach provider credentials, report/plan retention, artifact
publication or image decoding. No command gains remote authority from reading
a file.

`discover` reports the byte limits, regular-file requirement, rejected special
files, read-change behavior and unsupported-platform result. Default machine
inputs remain noninteractive. A caller finishes writing a regular request file
before invocation; no stdin or pipe protocol is implied.

## Validation

Focused coverage includes real subprocess FIFO rejection for the measurement
and image entry points, final-component swaps, mid-read removal/growth, released
descriptors, fixed error text, exact byte bounds, all compiler and artifact
input routes, strict scalar aliases, report failure before keys/ledger/provider,
and unchanged valid image/report workflows. These tests use only temporary
files, synthetic configuration and fake provider/credential boundaries.

The exact-snapshot validator additionally invokes FIFO rejection from the
installed wheel under a five-second deadline with provider/DNS/OS-store access
blocked. Expected failure codes and process exit statuses are checked
independently; a successful process cannot pass a check expecting exit 2. The
full Python suite has a bounded ten-minute deadline because the M26 exact suite
took 286 seconds under its prior five-minute limit. Other validation checks
retain their prior bounds. See [independent review](input-files-ultra-review.md)
and the separately recorded exact-source development evidence.
