# Declared video creative boundary

Reviewed: 2026-09-04
Status: **IMPLEMENTING / UNVERIFIED**

`src/adhelm/video_creative.py` adds a closed declaration model for video creative
identity without changing the existing image-asset schema or image digests.
`CustomerDeclaredVideoAsset.create()` accepts only bounded declared `video/mp4`
metadata and an opaque evidence reference. It computes a `video_asset:sha256:`
identity from the declared content digest, byte size, dimensions, duration, and
frame rate. It accepts neither media bytes nor a file path, opens no file, runs
no subprocess or decoder, and does not register an asset with a provider.

`CustomerDeclaredVideoRendition.from_video_asset()` derives a
`video_rendition:sha256:` identity from that asset identity, the serving-format
identifier, and locale. Locale belongs to the rendition rather than the
intrinsic asset identity. A change to declared bytes, dimensions, or duration
changes the asset identity and consequently changes the rendition identity.
Evidence provenance does not change either identity, since it is not proof that
the media bytes changed.

Both models explicitly state `CUSTOMER_DECLARED` and
`DECLARED_UNVERIFIED_NO_LOCAL_DECODE`. They also state that no frames, palette,
OCR, transcript, or audio analysis exists and that component-level performance
is not inferred. A later executor must require verified canonical video history
and current provider readback before it can use a declared video reference.

The TikTok compiler uses the declared VIDEO rendition directly in the creative
manifest. It does not use an image rendition as a video placeholder. A separate
poster image, if introduced later, must be explicitly declared with its own
role and identity.

Validation:

```text
uv run pytest tests/test_video_creative.py -q
```
