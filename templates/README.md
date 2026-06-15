# templates

Scene/layout templates referenced by `config.yaml → video_variants[].template`
(`timeline_reveal`, `split_stat`, `bracket_progression`, `player_card`,
`big_number`).

Templates are implemented as Pillow scene renderers in
[`video_builder.py`](../video_builder.py) (`RENDERERS`), so no external template
files are required to run. This folder is the place to add data-driven overrides
(JSON describing colours, positions, extra scenes) if you extend the renderer to
read them.
