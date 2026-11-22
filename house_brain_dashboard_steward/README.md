# House Brain Dashboard Steward

The read-only dashboard specialist of the House Brain AI family.

It watches Home Assistant's registries and dashboards and reports dashboard debt, such as missing entities, dead links and unsafe default taps. It notices new, renamed and replaced devices. With a local model on your own network, it drafts Lovelace YAML for you to review.

Since 0.1.1 its page also shows, against your main dashboard (`home-center`), which other dashboards are covered, which entities it shows are unavailable and for how long, Home Assistant's usage list per time of day, and a download of any dashboard's full config.

It never writes a dashboard or changes the house. It never calls a model outside your network.

It installs stopped (boot manual). Health is `AI_DEGRADED` until a local model is configured.
