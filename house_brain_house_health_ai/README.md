# House Brain House Health AI

The house-health member of the House Brain AI family (plan step 9, design R1). It answers "what do the
house's readings mean for the house": room temperatures drifting from their normal pattern, a slow
30-day mold index, the class of the last outage (power, internet or home network), how the UPS battery
is ageing, and when filters, batteries and life-safety devices are due. Heat safety, per-room mold,
leak-device lifecycle, Orbi/MoCA quality and the aquarium, fans and CO2 are named slots, built later.

It has no hands: no POST anywhere, no Home Assistant state write, no service call, no Supervisor API.
It raises no alerts of its own and runs in shadow beside the YAML packages it may one day replace.
Python standard library only; no private settings.

See [DOCS.md](DOCS.md).
