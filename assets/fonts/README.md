# assets/fonts

Optional. For on-brand typography, drop these two files here:

- `Inter-Bold.ttf`
- `Inter-Regular.ttf`

(or any TTF you license — Inter is free under the SIL Open Font License).

If absent, `video_builder.py` automatically falls back to **DejaVu Sans**
(installed in the Docker image via `fonts-dejavu-core`), then Liberation/Arial,
then Pillow's built-in font — so rendering never fails for a missing font.
